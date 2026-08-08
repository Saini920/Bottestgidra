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
log = logging.getLogger("worker_apktool_build")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        sys.exit(1)
    edit("🟢 Job started! Preparing Compiler on cloud server...", parse_mode="HTML")
    work_dir = Path(tempfile.gettempdir()) / ("apktool_build_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        dest = work_dir / "input.zip"
        last = [-100.0]

        dl_method = ["📥 Downloading ZIP..."]
        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            edit(f"{dl_method[0]}\n\n{progress_bar(pct)}")

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            got_file = False
            if TG_FILE_PATH:
                try:
                    filename = FILENAME or "download.zip"
                    tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                        async with client.stream("GET", tg_url) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length") or 0)
                            check_download_size(total)
                            done = 0
                            with open(dest, "wb") as fh:
                                async for chunk in resp.aiter_bytes(65536):
                                    if CANCELLED["v"]:
                                        raise JobCancelled()
                                    fh.write(chunk)
                                    done += len(chunk)
                                    if total:
                                        pct = min(100, int(done * 100 / total))
                                        await on_dl(pct)
                    got_file = True
                except Exception as http_err:
                    if not file_id:
                        raise
                    log.warning("HTTP download failed, falling back to MTProto: %s", http_err)
            if not got_file and file_id:
                filename = FILENAME or "download.zip"
                dl_method[0] = "📥 Downloading ZIP via MTProto (Pyrogram)..."
                await on_dl(0.0)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                dl_logs = []
                while True:
                    if CANCELLED["v"]:
                        proc.kill()
                        raise JobCancelled()
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").strip()
                    if line:
                        dl_logs.append(line)
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    err_tail = "\n".join(dl_logs[-8:]) or "no output"
                    raise ValueError(f"MTProto Download failed with code {proc.returncode}: {err_tail}")
                got_file = True
            if not got_file:
                await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
        except Exception as e:
            edit("❌ Download failed: " + str(e)[:300])
            return

        edit("📦 Extracting project...")
        proj_dir = work_dir / "project"
        proj_dir.mkdir()
        try:
            with zipfile.ZipFile(dest, "r") as zf:
                zf.extractall(proj_dir)
        except Exception as e:
            edit("❌ Extraction failed. Please send a valid ZIP containing an apktool project.")
            return

        # Find where apktool.yml is
        target_dir = None
        for root, dirs, files in os.walk(proj_dir):
            if "apktool.yml" in files:
                target_dir = Path(root)
                break
        
        if not target_dir:
            edit("❌ Missing `apktool.yml`! Could not find a valid apktool decompiled project in the ZIP.", keep_button=False)
            return

        unsigned_apk = work_dir / "unsigned.apk"
        cmd = ["java", "-Xmx8G", "-jar", "/opt/apktool/apktool.jar", "b", str(target_dir), "-o", str(unsigned_apk)]

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str):
            if pct - last_prog[0] < 5 and label == last_prog[1]: return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        await on_progress(5, "🔨 Starting Compiler...")
        
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        
        out_lines = []
        async def read_stream():
            while True:
                if CANCELLED["v"]:
                    proc.kill()
                    raise JobCancelled()
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode(errors="replace").strip()
                out_lines.append(line)
                low = line.lower()
                if "smali" in low:
                    await on_progress(30, "🧩 Compiling Smali Code...")
                elif "resources" in low or "xml" in low:
                    await on_progress(60, "🖼️ Building Resources and XML...")
                elif "copying" in low or "unknown" in low:
                    await on_progress(85, "📦 Packaging APK...")
            return await proc.wait()

        await read_stream()
        out_text = "\n".join(out_lines)
        
        if proc.returncode != 0 or not unsigned_apk.exists():
            error_file = work_dir / "error.txt"
            with open(error_file, "w") as f:
                f.write("Apktool Build Log:\n\n" + out_text)
            edit("❌ Compilation Failed! Check error.txt", keep_button=False)
            send_document(error_file, "❌ <b>Compilation Failed!</b>\nPlease check the `error.txt` file for syntax or resource errors.", "error.txt")
            return
            
        edit("🔑 Signing APK with Debug Keystore...")
        aligned_apk = work_dir / "aligned.apk"
        signed_apk = work_dir / "signed.apk"
        
        # Zipalign
        align_proc = await asyncio.create_subprocess_exec("zipalign", "-p", "4", str(unsigned_apk), str(aligned_apk))
        await align_proc.communicate()
        
        # Sign
        ks_path = "/opt/debug.keystore"
        if os.path.exists(ks_path):
            sign_proc = await asyncio.create_subprocess_exec("apksigner", "sign", "--ks", ks_path, "--ks-pass", "pass:android", str(aligned_apk))
            await sign_proc.communicate()
            if align_proc.returncode == 0 and sign_proc.returncode == 0:
                shutil.copy(aligned_apk, signed_apk)
        
        edit("📦 Packaging Signed & Unsigned APKs...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", FILENAME)[:60].replace(".zip", "") or "app"
        out_zip = work_dir / f"{safe_name}_compiled.zip"
        
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(unsigned_apk, f"{safe_name}_unsigned.apk")
            if signed_apk.exists():
                zf.write(signed_apk, f"{safe_name}_signed.apk")

        caption = f"✅ Compiled <b>{safe_name}</b> successfully!\nIncludes both Signed & Unsigned versions. — Powered By @R3V_X"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"✅ Compilation complete!\n📤 Sending ZIP...\n\n{progress_bar(pct)}")

        try:
            http_ok = False
            MAX_HTTP_UPLOAD = 50 * 1024 * 1024
            if out_zip.stat().st_size <= MAX_HTTP_UPLOAD:
                try:
                    with open(out_zip, "rb") as doc_f:
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
                    sys.executable, "upload_file.py", str(out_zip), caption,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                while True:
                    if CANCELLED["v"]:
                        proc.kill()
                        raise JobCancelled()
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
            edit("✅ Compilation complete! ZIP file delivered. 🔥", keep_button=False)
            
            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            edit(f"❌ Result ZIP ready, but upload failed: {e}", keep_button=False)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
    except Exception as e:
        import traceback
        err_msg = f"❌ Fatal crash in worker_apktool_build:\n<code>{traceback.format_exc()[-500:]}</code>"
        if BOT_TOKEN and CHAT_ID and MESSAGE_ID:
            try:
                httpx.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
                    data={"chat_id": CHAT_ID, "message_id": MESSAGE_ID, "text": err_msg, "parse_mode": "HTML"}
                )
            except: pass

