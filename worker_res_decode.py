import asyncio
import glob
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_res_decode")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "res.zip")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
USER_ID = os.environ.get("PAYLOAD_USER_ID", CHAT_ID)
REPORT_URL = os.environ.get("PAYLOAD_REPORT_URL", "")
REPORT_TOKEN = BOT_TOKEN
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


def check_download_size(total_bytes: int):
    if total_bytes and total_bytes > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
        raise ValueError(
            f"File is {total_bytes/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB."
        )


def notify_app(message: str, title: str = None):
    if not JOB_ID:
        return
    headers = {}
    if title:
        headers["Title"] = title.encode("utf-8")
    try:
        httpx.post(f"https://ntfy.sh/{JOB_ID}", data=message.encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        log.warning("Ntfy failed: %s", e)


def tg(method: str, **params):
    try:
        resp = httpx.post(f"{API}/{method}", data=params, timeout=60)
        return resp.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return None


import json


def edit(text: str, parse_mode: str = None, keep_button: bool = True):
    if CANCELLED["v"]:
        return
    params = {"chat_id": CHAT_ID, "message_id": MESSAGE_ID, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if keep_button:
        params["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": "🛑 Stop Processing", "callback_data": f"stop_{MESSAGE_ID}"}]]
        })
    tg("editMessageText", **params)
    notify_app(text)


def progress_bar(pct: float) -> str:
    val = float(pct)
    filled = max(0, min(16, int(val * 16 / 100)))
    bar = "▰" * filled + "▱" * (16 - filled)
    return f"{bar} {val:.2f} %"


# ==============================================================================
# AXML & ARSC Decoding Utilities
# ==============================================================================

def is_binary_xml(data: bytes) -> bool:
    if len(data) < 8:
        return False
    # Standard chunk header: type 0x0003, header size 0x0008
    return data[:4] == b"\x03\x00\x08\x00" or (data[0] == 0x03 and data[1] == 0x00)


def parse_arsc(data: bytes) -> dict:
    """Parses resources.arsc to map (package, type, entry) -> '@type/name'."""
    res_map = {}
    try:
        if len(data) < 12:
            return res_map
        c_type, header_size, chunk_size, package_count = struct.unpack_from("<HHII", data, 0)
        if c_type != 0x0002:
            return res_map

        offset = header_size
        if offset < len(data):
            st_type, st_hsize, st_size = struct.unpack_from("<HHI", data, offset)
            if st_type == 0x0001:
                offset += st_size

        while offset + 8 <= len(data):
            p_type, p_hsize, p_size = struct.unpack_from("<HHI", data, offset)
            if p_size <= 0:
                break
            if p_type == 0x0200:  # RES_TABLE_PACKAGE_TYPE
                pkg_data = data[offset : offset + p_size]
                pkg_id = struct.unpack_from("<I", pkg_data, 8)[0]
                type_strings_off, last_type, key_strings_off, last_key = struct.unpack_from("<IIII", pkg_data, 268)

                def read_sp(base_off):
                    strings = []
                    if base_off >= len(pkg_data):
                        return strings
                    st_type, st_hsize, st_size = struct.unpack_from("<HHI", pkg_data, base_off)
                    if st_type != 0x0001:
                        return strings
                    st_cnt, sty_cnt, flags, s_start, sy_start = struct.unpack_from("<IIIII", pkg_data, base_off + 8)
                    is_utf8 = (flags & (1 << 8)) != 0
                    offsets = struct.unpack_from(f"<{st_cnt}I", pkg_data, base_off + st_hsize)
                    for off in offsets:
                        p = base_off + s_start + off
                        if p >= len(pkg_data):
                            strings.append("")
                            continue
                        if is_utf8:
                            if pkg_data[p] & 0x80:
                                p += 2
                            else:
                                p += 1
                            u8len = pkg_data[p]
                            if u8len & 0x80:
                                u8len = ((u8len & 0x7F) << 8) | pkg_data[p + 1]
                                p += 2
                            else:
                                p += 1
                            strings.append(pkg_data[p : p + u8len].decode("utf-8", errors="replace"))
                        else:
                            u16len = struct.unpack_from("<H", pkg_data, p)[0]
                            if u16len & 0x8000:
                                u16len = ((u16len & 0x7FFF) << 16) | struct.unpack_from("<H", pkg_data, p + 2)[0]
                                p += 4
                            else:
                                p += 2
                            strings.append(pkg_data[p : p + u16len * 2].decode("utf-16le", errors="replace"))
                    return strings

                type_strings = read_sp(type_strings_off)
                key_strings = read_sp(key_strings_off)

                sub_off = max(type_strings_off, key_strings_off)
                if key_strings_off < len(pkg_data):
                    kst_type, kst_hsize, kst_size = struct.unpack_from("<HHI", pkg_data, key_strings_off)
                    sub_off = key_strings_off + kst_size

                while sub_off + 8 <= len(pkg_data):
                    sub_type, sub_hsize, sub_size = struct.unpack_from("<HHI", pkg_data, sub_off)
                    if sub_size <= 0:
                        break
                    if sub_type == 0x0201:  # RES_TABLE_TYPE_TYPE
                        t_id, t_res0, t_res1, entry_count, entries_start = struct.unpack_from("<BBHII", pkg_data, sub_off + 8)
                        type_name = type_strings[t_id - 1] if 0 < t_id <= len(type_strings) else f"type{t_id}"
                        entry_offsets = struct.unpack_from(f"<{entry_count}I", pkg_data, sub_off + sub_hsize)
                        for e_idx, e_off in enumerate(entry_offsets):
                            if e_off != 0xFFFFFFFF:
                                entry_pos = sub_off + entries_start + e_off
                                if entry_pos + 8 <= len(pkg_data):
                                    e_size, e_flags, key_idx = struct.unpack_from("<HHI", pkg_data, entry_pos)
                                    key_name = key_strings[key_idx] if 0 <= key_idx < len(key_strings) else f"entry_{e_idx}"
                                    res_id = (pkg_id << 24) | (t_id << 16) | e_idx
                                    res_map[res_id] = f"@{type_name}/{key_name}"
                    sub_off += sub_size
            offset += p_size
    except Exception as e:
        log.debug("ARSC parse notice: %s", e)
    return res_map


def format_attribute_value(data_type: int, data: int, string_pool: list, raw_val_idx: int = None, arsc_map: dict = None) -> str:
    if raw_val_idx is not None and string_pool and 0 <= raw_val_idx < len(string_pool) and raw_val_idx != 0xFFFFFFFF:
        val = string_pool[raw_val_idx]
        if val is not None and val != "":
            return val

    if data_type == 0x01:  # TYPE_REFERENCE
        if arsc_map and data in arsc_map:
            return arsc_map[data]
        return f"@0x{data:08x}"
    elif data_type == 0x02:  # TYPE_ATTRIBUTE
        if arsc_map and data in arsc_map:
            return f"?{arsc_map[data].lstrip('@')}"
        return f"?0x{data:08x}"
    elif data_type == 0x03:  # TYPE_STRING
        if string_pool and 0 <= data < len(string_pool):
            return string_pool[data]
        return str(data)
    elif data_type == 0x04:  # TYPE_FLOAT
        return f"{struct.unpack('<f', struct.pack('<I', data))[0]:g}"
    elif data_type == 0x05:  # TYPE_DIMENSION
        units = ["px", "dp", "sp", "pt", "in", "mm"]
        unit = units[data & 0x0F] if (data & 0x0F) < len(units) else ""
        radix = (data >> 4) & 0x03
        mantissa = struct.unpack("<i", struct.pack("<I", (data & 0xFFFFFF00)))[0] >> 8
        val = mantissa * (1.0 / (1 << [0, 7, 15, 23][radix]))
        return f"{val:g}{unit}"
    elif data_type == 0x06:  # TYPE_FRACTION
        units = ["%", "%p"]
        unit = units[data & 0x0F] if (data & 0x0F) < len(units) else ""
        radix = (data >> 4) & 0x03
        mantissa = struct.unpack("<i", struct.pack("<I", (data & 0xFFFFFF00)))[0] >> 8
        val = mantissa * (1.0 / (1 << [0, 7, 15, 23][radix])) * 100.0
        return f"{val:g}{unit}"
    elif data_type == 0x12:  # TYPE_INT_BOOLEAN
        return "true" if data != 0 else "false"
    elif data_type == 0x10:  # TYPE_INT_DEC
        return str(struct.unpack("<i", struct.pack("<I", data))[0])
    elif data_type == 0x11:  # TYPE_INT_HEX
        return f"0x{data:x}"
    elif 0x1C <= data_type <= 0x1F:  # TYPE_COLOR
        if data_type == 0x1D:  # RGB8
            return f"#{data & 0xFFFFFF:06x}"
        return f"#{data:08x}"
    return str(data)


def decode_axml_pure(data: bytes, arsc_map: dict = None) -> str:
    """Decodes Android Binary XML (AXML) into formatted plaintext XML."""
    if len(data) < 8:
        raise ValueError("Data too short for AXML")
    c_type, header_size, chunk_size = struct.unpack_from("<HHI", data, 0)
    if c_type != 0x0003:
        raise ValueError(f"Not an AXML chunk (type: 0x{c_type:04x})")

    offset = header_size
    string_pool = []
    namespaces = {}  # uri -> prefix
    root_nodes = []
    node_stack = []

    while offset + 8 <= len(data):
        c_type, c_hsize, c_size = struct.unpack_from("<HHI", data, offset)
        if c_size <= 0:
            break
        chunk_data = data[offset : offset + c_size]

        if c_type == 0x0001:  # RES_STRING_POOL_TYPE
            st_count, style_count, flags, strings_start, styles_start = struct.unpack_from("<IIIII", chunk_data, 8)
            is_utf8 = (flags & (1 << 8)) != 0
            offsets = struct.unpack_from(f"<{st_count}I", chunk_data, c_hsize)
            for off in offsets:
                p = strings_start + off
                if p >= len(chunk_data):
                    string_pool.append("")
                    continue
                if is_utf8:
                    if chunk_data[p] & 0x80:
                        p += 2
                    else:
                        p += 1
                    u8len = chunk_data[p]
                    if u8len & 0x80:
                        u8len = ((u8len & 0x7F) << 8) | chunk_data[p + 1]
                        p += 2
                    else:
                        p += 1
                    string_pool.append(chunk_data[p : p + u8len].decode("utf-8", errors="replace"))
                else:
                    u16len = struct.unpack_from("<H", chunk_data, p)[0]
                    if u16len & 0x8000:
                        u16len = ((u16len & 0x7FFF) << 16) | struct.unpack_from("<H", chunk_data, p + 2)[0]
                        p += 4
                    else:
                        p += 2
                    string_pool.append(chunk_data[p : p + u16len * 2].decode("utf-16le", errors="replace"))

        elif c_type == 0x0100:  # RES_XML_START_NAMESPACE_TYPE
            prefix_idx, uri_idx = struct.unpack_from("<II", chunk_data, 16)
            prefix = string_pool[prefix_idx] if 0 <= prefix_idx < len(string_pool) else ""
            uri = string_pool[uri_idx] if 0 <= uri_idx < len(string_pool) else ""
            if uri:
                namespaces[uri] = prefix

        elif c_type == 0x0102:  # RES_XML_START_ELEMENT_TYPE
            ns_idx, name_idx = struct.unpack_from("<II", chunk_data, 16)
            attr_start, attr_size, attr_count = struct.unpack_from("<HHH", chunk_data, 24)
            tag_name = string_pool[name_idx] if 0 <= name_idx < len(string_pool) else "unknown"

            attrs = []
            attr_offset = 16 + attr_start
            for _ in range(attr_count):
                if attr_offset + 20 > len(chunk_data):
                    break
                a_ns, a_name, a_val_idx, a_sz, a_res, a_dt, a_data = struct.unpack_from("<IIIHBB I", chunk_data, attr_offset)
                attr_offset += attr_size

                name_str = string_pool[a_name] if 0 <= a_name < len(string_pool) else "unknown"
                ns_str = string_pool[a_ns] if (0 <= a_ns < len(string_pool) and a_ns != 0xFFFFFFFF) else ""
                prefix = namespaces.get(ns_str, "")
                attr_key = f"{prefix}:{name_str}" if prefix else name_str
                val = format_attribute_value(a_dt, a_data, string_pool, a_val_idx, arsc_map)
                val_escaped = val.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
                attrs.append(f'{attr_key}="{val_escaped}"')

            node = {"name": tag_name, "attrs": attrs, "children": [], "cdata": []}
            if node_stack:
                node_stack[-1]["children"].append(node)
            else:
                root_nodes.append(node)
            node_stack.append(node)

        elif c_type == 0x0103:  # RES_XML_END_ELEMENT_TYPE
            if node_stack:
                node_stack.pop()

        elif c_type == 0x0104:  # RES_XML_CDATA_TYPE
            data_idx = struct.unpack_from("<I", chunk_data, 16)[0]
            cdata_str = string_pool[data_idx] if 0 <= data_idx < len(string_pool) else ""
            if node_stack and cdata_str.strip():
                node_stack[-1]["cdata"].append(cdata_str.strip())

        offset += c_size

    def node_to_lines(node, indent=0, is_root=False):
        space = "    " * indent
        tag = node["name"]
        attrs = list(node["attrs"])
        if is_root and namespaces:
            for uri, prefix in sorted(namespaces.items(), key=lambda x: x[1]):
                xmlns = f'xmlns:{prefix}="{uri}"' if prefix else f'xmlns="{uri}"'
                attrs.insert(0, xmlns)

        attr_str = (" " + " ".join(attrs)) if attrs else ""
        if not node["children"] and not node["cdata"]:
            return [f"{space}<{tag}{attr_str} />"]

        lines = [f"{space}<{tag}{attr_str}>"]
        for cd in node["cdata"]:
            lines.append(f"{space}    {cd}")
        for ch in node["children"]:
            lines.extend(node_to_lines(ch, indent + 1, False))
        lines.append(f"{space}</{tag}>")
        return lines

    output_lines = ['<?xml version="1.0" encoding="utf-8"?>']
    for root in root_nodes:
        output_lines.extend(node_to_lines(root, indent=0, is_root=True))

    return "\n".join(output_lines) + "\n"


def decode_xml_file(xml_path: Path, arsc_map: dict = None) -> bool:
    """Decodes a single XML file if binary. Returns True if converted."""
    try:
        with open(xml_path, "rb") as f:
            raw = f.read()
        if not is_binary_xml(raw):
            return False

        # Try androguard / pyaxmlparser if available
        decoded_text = None
        try:
            from androguard.core.bytecodes.axml import AXMLPrinter
            ap = AXMLPrinter(raw)
            if ap.is_valid():
                xml_b = ap.get_xml()
                if xml_b:
                    decoded_text = xml_b.decode("utf-8", errors="replace")
        except Exception:
            pass

        if not decoded_text:
            decoded_text = decode_axml_pure(raw, arsc_map)

        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(decoded_text)
        return True
    except Exception as e:
        log.warning("Failed to decode XML '%s': %s", xml_path.name, e)
        return False


# ==============================================================================
# Worker Execution
# ==============================================================================

async def main():
    if not BOT_TOKEN or not CHAT_ID or not MESSAGE_ID:
        log.error("Missing required env vars: BOT_TOKEN, CHAT_ID, MESSAGE_ID")
        sys.exit(1)

    start_time = time.time()
    work_dir = Path(tempfile.mkdtemp(prefix="worker_res_decode_"))
    try:
        edit("⏳ <b>Starting Res Decoder Worker...</b>", parse_mode="HTML")
        dest = work_dir / Path(FILENAME).name

        # ----------------------------------------------------------------------
        # Step 1: Download
        # ----------------------------------------------------------------------
        use_mtproto = bool(os.environ.get("PAYLOAD_FILE_ID", "").strip())
        if use_mtproto:
            edit("📥 <b>Downloading resource archive (MTProto)...</b>", parse_mode="HTML")
            last_down = [0]

            def on_down(pct: float):
                ipct = int(pct)
                if ipct < last_down[0] or ipct - last_down[0] < 5:
                    return
                last_down[0] = ipct
                edit(f"📥 <b>Downloading resource archive...</b>\n{progress_bar(pct)}", parse_mode="HTML")

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "download_file.py", str(dest),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            while True:
                if CANCELLED["v"]:
                    proc.kill()
                    raise JobCancelled()
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode(errors="replace").strip()
                if line_str.startswith("PROGRESS:"):
                    try:
                        pct = float(line_str.split(":")[1])
                        on_down(pct)
                    except ValueError:
                        pass
            await proc.wait()
            if proc.returncode != 0:
                raise ValueError(f"MTProto download failed (exit code {proc.returncode})")
        else:
            download_url = FILE_URL
            if not download_url and TG_FILE_PATH:
                download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{TG_FILE_PATH}"
            if not download_url:
                raise ValueError("No download URL or file_id provided.")

            edit("📥 <b>Downloading resource archive (HTTP)...</b>", parse_mode="HTML")
            last_down = [0]
            async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as client:
                async with client.stream("GET", download_url) as resp:
                    resp.raise_for_status()
                    total_bytes = int(resp.headers.get("content-length", 0))
                    check_download_size(total_bytes)
                    downloaded = 0
                    with open(dest, "wb") as f:
                        async for chunk in resp.aiter_bytes(65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_bytes > 0:
                                pct = downloaded * 100.0 / total_bytes
                                ipct = int(pct)
                                if ipct >= last_down[0] + 5:
                                    last_down[0] = ipct
                                    edit(f"📥 <b>Downloading archive...</b>\n{progress_bar(pct)}", parse_mode="HTML")

        # ----------------------------------------------------------------------
        # Step 2: Unzip Archive
        # ----------------------------------------------------------------------
        edit("📦 <b>Extracting archive contents...</b>", parse_mode="HTML")
        unpacked_dir = work_dir / "unpacked"
        unpacked_dir.mkdir(parents=True, exist_ok=True)

        if zipfile.is_zipfile(dest):
            with zipfile.ZipFile(dest, "r") as zf:
                zf.extractall(unpacked_dir)
        else:
            # Single file or uncompressed file
            shutil.copy2(dest, unpacked_dir / dest.name)

        # ----------------------------------------------------------------------
        # Step 3: Scan and Decode XMLs
        # ----------------------------------------------------------------------
        arsc_map = {}
        arsc_file = next(unpacked_dir.rglob("resources.arsc"), None)
        if arsc_file and arsc_file.exists():
            log.info("Found resources.arsc at %s, parsing string pool...", arsc_file)
            try:
                with open(arsc_file, "rb") as af:
                    arsc_map = parse_arsc(af.read())
                log.info("Loaded %d resource mappings from resources.arsc", len(arsc_map))
            except Exception as e:
                log.warning("Could not parse resources.arsc: %s", e)

        # Collect candidate XML files
        candidate_files = []
        for p in unpacked_dir.rglob("*"):
            if p.is_file():
                if p.suffix.lower() == ".xml" or is_binary_xml(p.read_bytes()[:16]):
                    candidate_files.append(p)

        total_candidates = len(candidate_files)
        edit(f"🎨 <b>Found {total_candidates} XML files. Decoding resources...</b>\n{progress_bar(0)}", parse_mode="HTML")

        decoded_count = 0
        last_progress_pct = 0
        for idx, xml_p in enumerate(candidate_files, start=1):
            if CANCELLED["v"]:
                raise JobCancelled()
            if decode_xml_file(xml_p, arsc_map):
                decoded_count += 1
            cur_pct = int(idx * 100 / max(1, total_candidates))
            if cur_pct >= last_progress_pct + 10 or idx == total_candidates:
                last_progress_pct = cur_pct
                edit(
                    f"🎨 <b>Decoding Android Resources ({idx}/{total_candidates})...</b>\n"
                    f"Decoded: <b>{decoded_count}</b> binary XMLs\n\n"
                    f"{progress_bar(cur_pct)}",
                    parse_mode="HTML",
                )

        # ----------------------------------------------------------------------
        # Step 4: Package Output ZIP
        # ----------------------------------------------------------------------
        edit("📦 <b>Packaging decoded resources into ZIP...</b>", parse_mode="HTML")
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", FILENAME)[:60] or "res"
        orig_stem = Path(safe_name).stem or "resources"
        out_zip = work_dir / f"decoded_res_{orig_stem}.zip"

        total_files = 0
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(unpacked_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, unpacked_dir)
                    zf.write(fp, arcname)
                    total_files += 1

        elapsed = time.time() - start_time
        caption = (
            f"🎨 <b>Res Decode Complete!</b>\n\n"
            f"📁 <b>Archive:</b> <code>{safe_name}</code>\n"
            f"📊 <b>Total Files:</b> <b>{total_files}</b>\n"
            f"📝 <b>Decoded XMLs:</b> <b>{decoded_count}</b>\n"
            f"⏱️ <b>Time Taken:</b> <b>{elapsed:.1f}s</b>\n\n"
            f"⚡ <i>Powered By @R3V_X</i>"
        )

        # ----------------------------------------------------------------------
        # Step 5: Upload Output ZIP
        # ----------------------------------------------------------------------
        edit("📤 <b>Uploading decoded resource ZIP...</b>", parse_mode="HTML")
        http_ok = False
        MAX_HTTP_UPLOAD = 50 * 1024 * 1024
        if out_zip.stat().st_size <= MAX_HTTP_UPLOAD:
            try:
                with open(out_zip, "rb") as doc_f:
                    url = f"{API}/sendDocument"
                    data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"}
                    files = {"document": (out_zip.name, doc_f, "application/zip")}
                    async with httpx.AsyncClient(timeout=300) as client:
                        resp = await client.post(url, data=data, files=files)
                        resp.raise_for_status()
                http_ok = True
            except Exception as e:
                log.warning("HTTP upload failed, falling back to MTProto: %s", e)

        if not http_ok:
            if not os.environ.get("API_ID", "").strip():
                raise ValueError("File too large for Bot API (50MB) and no API_ID/API_HASH configured.")

            up_last = [0]
            def on_up(pct: float):
                ipct = int(pct)
                if ipct < up_last[0] or ipct - up_last[0] < 5:
                    return
                up_last[0] = ipct
                edit(f"📤 <b>Uploading decoded ZIP (MTProto)...</b>\n{progress_bar(pct)}", parse_mode="HTML")

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", str(out_zip), caption,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            while True:
                if CANCELLED["v"]:
                    proc.kill()
                    raise JobCancelled()
                line = await proc.stdout.readline()
                if not line:
                    break
                line_str = line.decode(errors="replace").strip()
                if line_str.startswith("PROGRESS:"):
                    try:
                        pct = float(line_str.split(":")[1])
                        on_up(pct)
                    except ValueError:
                        pass
            await proc.wait()
            if proc.returncode != 0:
                raise ValueError(f"MTProto upload failed (code {proc.returncode})")

        edit("✅ <b>Res Decode Complete!</b> ZIP file delivered. 🔥", parse_mode="HTML", keep_button=False)
        if JOB_ID:
            notify_app("FINAL_ZIP_URL:telegram_direct_upload")

    except JobCancelled:
        edit("🚫 <b>Job cancelled by user.</b>", parse_mode="HTML", keep_button=False)
    except Exception as e:
        log.exception("Worker error")
        edit(f"❌ <b>Res Decode Failed:</b> <code>{str(e)[:300]}</code>", parse_mode="HTML", keep_button=False)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
