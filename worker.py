import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
GHIDRA_HOME = Path(os.environ.get("GHIDRA_HOME", "/opt/ghidra"))
ANALYZE_HEADLESS = GHIDRA_HOME / "support" / "analyzeHeadless"
SCRIPT_DIR = Path(__file__).resolve().parent / "ghidra_scripts"
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "500"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def tg(method: str, **params):
    try:
        resp = httpx.post(f"{API}/{method}", data=params, timeout=60)
        return resp.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return None


def edit(text: str, parse_mode: str = None):
    params = {"chat_id": CHAT_ID, "message_id": MESSAGE_ID, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    tg("editMessageText", **params)


def progress_bar(pct: float) -> str:
    val = float(pct)
    filled = max(0, min(16, int(val * 16 / 100)))
    bar = "▰" * filled + "▱" * (16 - filled)
    return f"{bar} {val:.2f} %"


def apply_memory_settings():
    mem = os.environ.get("JAVA_MAX_MEM", "4G")
    props = GHIDRA_HOME / "support" / "launch.properties"
    try:
        text = props.read_text(errors="replace")
        new = re.sub(r"^JAVA_MAX_MEM\s*=.*$", f"JAVA_MAX_MEM={mem}", text, flags=re.M)
        if new == text and "JAVA_MAX_MEM" not in text:
            new = text.rstrip("\n") + f"\nJAVA_MAX_MEM={mem}\n"
        if new != text:
            props.write_text(new)
        log.info("JAVA_MAX_MEM set to %s", mem)
    except Exception as e:
        log.warning("Could not set JAVA_MAX_MEM: %s", e)


def resolve_drive_url(url: str):
    if "drive.google.com" not in url:
        return url
    m = re.search(r"/file/d/([^/?#]+)", url)
    if not m:
        m = re.search(r"[?&]id=([^&#]+)", url)
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1)}"
    return url


async def download_url(url: str, dest: Path, on_progress) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    fid = None
    if "drive.google.com" in url:
        m = re.search(r"/file/d/([^/?#]+)", url) or re.search(r"[?&]id=([^&#]+)", url)
        if m:
            fid = m.group(1)
            url = f"https://drive.google.com/uc?export=download&id={fid}"

    timeout = httpx.Timeout(30.0, connect=30.0, read=300.0, write=300.0)
    transport = httpx.AsyncHTTPTransport(retries=3)
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout, transport=transport) as client:
        for attempt in range(3):
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "text/html" in ct:
                    if attempt == 2:
                        raise ValueError("The link is a webpage, not a direct file.")
                    html = (await resp.aread()).decode(errors="replace")
                    if fid:
                        m = re.search(r'name="confirm"\s+value="([^"]+)"', html)
                        if m:
                            url = (f"https://drive.usercontent.google.com/download"
                                   f"?id={fid}&export=download&confirm={m.group(1)}")
                            continue
                        if "Google Drive" in html or "drive.google" in html:
                            raise ValueError("Google Drive file not accessible (check link permission / sharing).")
                    m = re.search(r'href="(https?://download[0-9]+\.mediafire\.com/[^"]+)"', html)
                    if m:
                        url = unescape(m.group(1))
                        continue
                    raise ValueError("The link is a webpage, not a direct file.")

                filename = "download.bin"
                cd = resp.headers.get("content-disposition", "")
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    filename = unquote(m.group(1)).strip()
                else:
                    path_part = unquote(resp.url.path.rstrip("/").rsplit("/", 1)[-1])
                    if path_part:
                        filename = path_part

                total = int(resp.headers.get("content-length") or 0)
                downloaded = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(65536):
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = min(100, int(downloaded * 100 / total))
                            await on_progress(pct)
                return filename
        raise ValueError("Could not download file from this link.")


async def run_ghidra(file_path: Path, work_dir: Path, on_progress) -> dict:
    project_dir = work_dir / "project"
    project_dir.mkdir(parents=True)
    out_c = work_dir / "decompiled.c"
    out_meta = work_dir / "info.txt"

    cmd = [
        str(ANALYZE_HEADLESS),
        str(project_dir),
        "Proj",
        "-overwrite",
        "-import", str(file_path),
        "-scriptPath", str(SCRIPT_DIR),
        "-postScript", "DecompileAll.java",
        str(out_c), str(out_meta),
        "-deleteProject",
    ]
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    tail = []
    await on_progress(5, "📥 Importing file into Ghidra...")

    async def read_stream():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            tail.append(line)
            del tail[:-60]
            low = line.lower()
            if "analyzing" in low or "processing" in low:
                await on_progress(20, "🔧 Analyzing binary with Ghidra...")
            m = re.search(r"DECOMP_PROGRESS\s+(\d+)/(\d+)", line)
            if m:
                done, total = int(m.group(1)), int(m.group(2))
                pct = int(20 + 75 * (done / total)) if total else 20
                await on_progress(pct, f"🧠 Decompiling functions {done}/{total}...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=3600)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("Ghidra analysis timed out")
    log.info("analyzeHeadless exit=%s", rc)
    return {"c": out_c, "meta": out_meta, "tail": "\n".join(tail[-40:]), "returncode": rc}


def send_document(file_path: Path, caption: str, filename: str):
    with open(file_path, "rb") as fh:
        resp = httpx.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:1024]},
            files={"document": (filename, fh, "application/zip")},
            timeout=180,
        )
    return resp.json()


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing Ghidra engine on <b>7GB RAM</b> cloud server...", parse_mode="HTML")
    apply_memory_settings()

    work_dir = Path(tempfile.gettempdir()) / ("ghidra_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        dest = work_dir / "input.bin"
        last = [0]

        async def on_dl(pct: int):
            if pct < last[0] or pct - last[0] < 2:
                return
            last[0] = pct
            edit(f"📥 Downloading file...\n{progress_bar(pct)} {pct}%")

        try:
            if TG_FILE_PATH:
                filename = FILENAME or "download.bin"
                tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                    async with client.stream("GET", tg_url) as resp:
                        resp.raise_for_status()
                        total = int(resp.headers.get("content-length") or 0)
                        downloaded = 0
                        with open(dest, "wb") as fh:
                            async for chunk in resp.aiter_bytes(65536):
                                fh.write(chunk)
                                downloaded += len(chunk)
                                if total:
                                    pct = min(100, int(downloaded * 100 / total))
                                    await on_dl(pct)
            else:
                filename = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
        except Exception as e:
            edit("❌ Download failed: " + str(e)[:300])
            return

        size = dest.stat().st_size
        if size == 0:
            edit("❌ Downloaded file is empty.")
            return
        if size > MAX_DOWNLOAD_MB * 1024 * 1024:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.")
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Ghidra analysis...")

        last = [0, ""]

        async def on_progress(pct: int, label: str = "🧠 Analyzing..."):
            if pct - last[0] < 5 and label == last[1]:
                return
            last[0], last[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)} {pct}%")

        try:
            result = await asyncio.wait_for(
                run_ghidra(dest, work_dir / "analysis", on_progress), timeout=3900
            )
        except TimeoutError:
            edit("⏰ Timeout! The file is too big or packed.")
            return
        except Exception as e:
            log.exception("analysis crashed")
            edit("❌ Analysis crashed: " + str(e)[:300])
            return

        out_files = []
        for fp in [result["c"], result["meta"]]:
            if fp.exists() and fp.stat().st_size > 0:
                out_files.append(fp)

        if not out_files:
            tail = result["tail"][-600:]
            if "Killed" in tail or "OutOfMemory" in tail or "insufficient memory" in tail.lower():
                edit("💥 Out of memory even on 7GB! The binary is extremely large or packed. Try a smaller file.")
            else:
                edit("❌ Analysis failed. Format might not be supported.\n" + tail[-300:])
            return

        edit("📦 Packaging results...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "decompiled"

        zip_path = work_dir / f"{orig_stem}_decompiled.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for fp in out_files:
                if fp.name == "decompiled.c":
                    arcname = f"{orig_stem}.c"
                elif fp.name == "info.txt":
                    arcname = f"{orig_stem}_info.txt"
                else:
                    arcname = f"{orig_stem}_{fp.name}"
                zf.write(fp, arcname)

        edit("✅ Decompilation complete! Sending ZIP...")
        resp = send_document(
            zip_path,
            f"✅ Decompiled <b>{safe_name}</b> with Ghidra on 7GB Cloud Server — Powered By @Ghostofhackers",
            f"{orig_stem}_decompiled.zip",
        )
        if resp and resp.get("ok"):
            edit("✅ Decompilation complete! ZIP file delivered. 🔥")
        else:
            edit("❌ Result ZIP ready, but Telegram send failed. Try again.")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
