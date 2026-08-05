import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_jadx")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 100

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


def edit(text: str, parse_mode: str = None, keep_button: bool = True):
    params = {"chat_id": CHAT_ID, "message_id": MESSAGE_ID, "text": text}
    if parse_mode:
        params["parse_mode"] = parse_mode
    if keep_button:
        params["reply_markup"] = json.dumps({
            "inline_keyboard": [[{"text": "🛑 Stop Processing", "callback_data": f"stop_{MESSAGE_ID}"}]]
        })
    tg("editMessageText", **params)
    notify_app(text)


import json


def progress_bar(pct: float) -> str:
    val = float(pct)
    filled = max(0, min(16, int(val * 16 / 100)))
    bar = "▰" * filled + "▱" * (16 - filled)
    return f"{bar} {val:.2f} %"


async def send_error_log(work_dir, exception_obj, title="JADX Decompilation failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    try:
        err_file = Path(work_dir) / "error.txt"
        err_file.write_text(f"❌ {title}:\n\n{err_str}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "upload_file.py", str(err_file), f"❌ Error Log:\n{str(exception_obj)[:100]}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.wait()
    except Exception as e:
        log.error("Failed to upload error log: %s", e)
    edit(f"❌ {title}. Error log sent.", keep_button=False)


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
                            url = f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm={m.group(1)}"
                            continue
                        if "Google Drive" in html or "drive.google" in html:
                            raise ValueError("Google Drive file not accessible.")
                    m = re.search(r'href="(https?://download[0-9]+\.mediafire\.com/[^"]+)"', html)
                    if m:
                        url = unescape(m.group(1))
                        continue
                    raise ValueError("The link is a webpage, not a direct file.")

                filename = "download.apk"
                cd = resp.headers.get("content-disposition", "")
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    filename = unquote(m.group(1)).strip()
                else:
                    path_part = unquote(resp.url.path.rstrip("/").rsplit("/", 1)[-1])
                    if path_part:
                        filename = path_part

                total = int(resp.headers.get("content-length") or 0)
                check_download_size(total)
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


async def run_jadx(file_path: Path, work_dir: Path, on_progress) -> Path:
    out_dir = work_dir / "jadx_out"

    target_file = file_path
    if file_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(file_path, "r") as zf:
            dex_files = [n for n in zf.namelist() if n.lower().endswith((".dex", ".smali", ".class", ".jar"))]
            if dex_files:
                extract_dir = work_dir / "extracted_src"
                extract_dir.mkdir(exist_ok=True)
                for df in dex_files:
                    zf.extract(df, extract_dir)
                target_file = extract_dir
            elif any(n.lower().endswith(".apk") for n in zf.namelist()):
                apk_files = [n for n in zf.namelist() if n.lower().endswith(".apk")]
                extract_dir = work_dir / "extracted_apk"
                extract_dir.mkdir(exist_ok=True)
                zf.extract(apk_files[0], extract_dir)
                target_file = extract_dir / apk_files[0]

    cmd = [
        "/opt/jadx/bin/jadx",
        "-d", str(out_dir),
        "--no-res",
        str(target_file)
    ]
    log.info("Running JADX: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    await on_progress(10, "☕ Starting JADX Decompiler...")

    out_lines = []
    async def read_stream():
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            out_lines.append(line)
            if "processing" in line.lower() or "progress" in line.lower():
                await on_progress(50, "☕ Decompiling DEX to Java...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=7200)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("JADX decompilation timed out")

    if rc != 0 or not out_dir.exists():
        err_msg = "\n".join(out_lines[-20:])
        if "No classes for decompile" in err_msg:
            raise ValueError("No Java code found! This file contains no classes/DEX to decompile.")
        raise RuntimeError(f"JADX failed with exit code {rc}:\n{err_msg}")

    return out_dir


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing JADX on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("jadx_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        ext = Path(FILENAME).suffix or ".apk"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            is_mtproto = bool(os.environ.get("PAYLOAD_FILE_ID", ""))
            dl_text = "📥 Downloading via MTProto (Pyrogram)..." if is_mtproto else "📥 Downloading file..."
            edit(f"{dl_text}\n\n{progress_bar(pct)}")

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            if file_id:
                filename = FILENAME or "download.bin"
                await on_dl(0.0)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Download failed with code {proc.returncode}")
            elif TG_FILE_PATH:
                filename = FILENAME or "download.apk"
                tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                    async with client.stream("GET", tg_url) as resp:
                        resp.raise_for_status()
                        total = int(resp.headers.get("content-length") or 0)
                        check_download_size(total)
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
            await send_error_log(work_dir, e, "Download failed")
            return

        size = dest.stat().st_size
        if size == 0:
            edit("❌ Downloaded file is empty.", keep_button=False)
            return
        if size > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting JADX decompilation...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "☕ Decompiling Java..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            out_dir = await run_jadx(dest, work_dir, on_progress)
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The file is too big for JADX.", keep_button=False)
            return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "JADX Decompilation crashed")
            return

        edit("📦 Packaging Java Source Code...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "jadx"

        zip_path = work_dir / f"{orig_stem}_jadx_java.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(out_dir):
                for f in files:
                    file_path = os.path.join(root, f)
                    arcname = os.path.relpath(file_path, out_dir)
                    zf.write(file_path, arcname)

        edit("✅ JADX Decompilation complete! Sending ZIP...")

        caption = f"✅ Decompiled <b>{safe_name}</b> to Java Source Code with JADX — Powered By @Ghostofhackers & @R3V_X"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"✅ Decompilation complete!\n📤 Sending ZIP...\n\n{progress_bar(pct)}")

        try:
            http_ok = False
            MAX_HTTP_UPLOAD = 50 * 1024 * 1024
            if zip_path.stat().st_size <= MAX_HTTP_UPLOAD:
                try:
                    with open(zip_path, "rb") as doc_f:
                        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
                        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
                        files = {"document": doc_f}
                        async with httpx.AsyncClient(timeout=300) as client:
                            resp = await client.post(url, data=data, files=files)
                            resp.raise_for_status()
                    http_ok = True
                except Exception as e:
                    log.warning("HTTP upload failed, falling back to MTProto: %s", e)
            if not http_ok:
                if not os.environ.get("API_ID", "").strip():
                    raise ValueError("File too large for Bot API (50MB) and no API_ID/API_HASH configured.")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", str(zip_path), caption,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = int(float(line.split(":")[1]))
                            await on_up(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Upload failed with code {proc.returncode}")
            edit("✅ Decompilation complete! Java ZIP file delivered. 🔥", keep_button=False)

            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
