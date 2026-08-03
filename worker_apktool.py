import asyncio
import logging
import os
import re
import shutil
import zipfile
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote
from html import unescape

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_apktool")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
MAX_DOWNLOAD_MB = int(os.environ.get("MAX_DOWNLOAD_MB", "500"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


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


def upload_gofile(file_path: Path) -> str:
    token = "j7HmWBxOe5wamBhhg4gb9DOwCN5WzOKh"
    try:
        with httpx.Client(timeout=180) as client:
            r = client.get("https://api.gofile.io/servers")
            servers = r.json().get("data", {}).get("servers", [])
            if not servers: return ""
            server = servers[0]["name"]
            
            with open(file_path, "rb") as fh:
                r = client.post(
                    f"https://{server}.gofile.io/contents/uploadfile",
                    data={"token": token},
                    files={"file": (file_path.name, fh)}
                )
                return r.json().get("data", {}).get("downloadPage", "")
    except Exception as e:
        log.warning("GoFile upload failed: %s", e)
    return ""


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
    notify_app(text)


def progress_bar(pct: float) -> str:
    val = float(pct)
    filled = max(0, min(16, int(val * 16 / 100)))
    bar = "▰" * filled + "▱" * (16 - filled)
    return f"{bar} {val:.2f} %"


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


async def run_apktool(file_path: Path, work_dir: Path, on_progress) -> Path:
    out_dir = work_dir / "decompiled_apk"
    cmd = [
        "java", "-jar", "/opt/apktool/apktool.jar", "d", str(file_path),
        "-o", str(out_dir), "-f"
    ]
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    
    await on_progress(10, "📱 Decompiling APK with Apktool...")
    
    async def read_stream():
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            low = line.lower()
            if "baksmali" in low or "smali" in low:
                await on_progress(40, "🧩 Decompiling Smali Code...")
            elif "resources" in low or "xml" in low:
                await on_progress(70, "🖼️ Decoding Resources and XML...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=1800)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("Apktool analysis timed out")
    
    if rc != 0 or not out_dir.exists():
        raise Exception(f"Apktool failed with return code {rc}")
    
    return out_dir


def send_document(file_path: Path, caption: str, filename: str):
    with open(file_path, "rb") as fh:
        resp = httpx.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"document": (filename, fh, "application/zip")},
            timeout=180,
        )
    return resp.json()


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing Apktool on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("apktool_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        dest = work_dir / "input.apk"
        last = [0]

        async def on_dl(pct: int):
            if pct < last[0] or pct - last[0] < 2:
                return
            last[0] = pct
            edit(f"📥 Downloading...\n{progress_bar(pct)}")

        try:
            if TG_FILE_PATH:
                dl_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"https://api.telegram.org/file/bot{BOT_TOKEN}/{TG_FILE_PATH}"
                edit("📥 Downloading from Telegram...")
                async with httpx.AsyncClient(timeout=300.0) as client:
                    async with client.stream("GET", dl_url) as resp:
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
                filename_dl = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
        except Exception as e:
            edit("❌ Download failed: " + str(e)[:300])
            return

        size = dest.stat().st_size
        if size == 0:
            edit("❌ Downloaded file is empty.")
            return
        if size > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.")
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Apktool analysis...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "🧠 Analyzing..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            out_dir = await run_apktool(dest, work_dir, on_progress)
            bname = Path(FILENAME).stem or "decompiled_apk"
        except TimeoutError:
            edit("⏰ Timeout! The APK is too big.")
            return
        except Exception as e:
            log.exception("Apktool crashed")
            edit("❌ Apktool failed: " + str(e)[:300])
            return

        edit("📦 Packaging results...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", FILENAME)[:60] or "file"
        orig_stem = Path(safe_name).stem or "decompiled"

        zip_path = work_dir / f"{orig_stem}_apktool.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(out_dir):
                for f in files:
                    file_path = os.path.join(root, f)
                    arcname = os.path.relpath(file_path, out_dir)
                    zf.write(file_path, arcname)

        edit("✅ Decompilation complete! Sending ZIP...")
        
        if JOB_ID:
            gofile_url = upload_gofile(zip_path)
            if gofile_url:
                notify_app(f"FINAL_ZIP_URL:{gofile_url}")
            else:
                notify_app("❌ GoFile upload failed for app.")

        if zip_path.stat().st_size > 49_000_000:
            edit("📦 File is larger than 50MB. Uploading to GoFile Cloud...")
            gofile_url = upload_gofile(zip_path)
            if gofile_url:
                edit(f"✅ <b>Decompilation Complete!</b>\\n\\n📁 <b>File:</b> <code>{orig_stem}_apktool.zip</code>\\n📦 <b>Size:</b> {zip_path.stat().st_size / (1024*1024):.2f} MB\\n\\n☁️ <b>Download Link:</b>\\n{gofile_url}\\n\\n<i>⚡ Powered By @Ghostofhackers & @R3V_X</i>", parse_mode="HTML")
            else:
                edit("❌ Result ZIP is too large for Telegram, and GoFile upload failed.")
        else:
            resp = send_document(
                zip_path,
                f"✅ Decompiled <b>{safe_name}</b> with Apktool — Powered By @Ghostofhackers & @R3V_X",
                f"{orig_stem}_apktool.zip",
            )
            if resp and resp.get("ok"):
                edit("✅ Decompilation complete! ZIP file delivered. 🔥")
            else:
                edit("❌ Result ZIP ready, but Telegram send failed. Try again.")

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
