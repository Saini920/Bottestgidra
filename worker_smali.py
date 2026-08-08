import asyncio
import glob
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_smali")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "download")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
USER_ID = os.environ.get("PAYLOAD_USER_ID", CHAT_ID)
REPORT_URL = os.environ.get("PAYLOAD_REPORT_URL", "")
REPORT_TOKEN = BOT_TOKEN
MODE = os.environ.get("PAYLOAD_SMALI_MODE", "full")
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

BAKSMALI_JAR = "/opt/baksmali.jar"
MAX_DEX_FREE = 3
MAX_DEX_PREMIUM = 10

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)
    edit("🟢 Job started! Preparing Smali Decode engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("smali_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        ext = Path(FILENAME).suffix or ".dex"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        dl_method = ["📥 Downloading file..."]
        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            edit(f"{dl_method[0]}\n\n{progress_bar(pct)}")

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            got_file = False
            if TG_FILE_PATH:
                try:
                    filename = FILENAME or "download.dex"
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
                filename = FILENAME or "download.dex"
                dl_method[0] = "📥 Downloading via MTProto (Pyrogram)..."
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

        try:
            extra = count_zip_so_dex(dest)
        except Exception as e:
            log.warning("Could not count zip contents: %s", e)
            extra = 0
        if extra:
            report_extra_count(extra)

        try:
            check_zip_limits(dest)
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Smali Decode...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "🧩 Decoding to Smali..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            dex_inputs = collect_dex_inputs(dest, work_dir)
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return

        smali_dirs = []
        total_dex = len(dex_inputs)
        try:
            for i, dex_path in enumerate(dex_inputs, start=1):
                out_dir = work_dir / f"smali_{i}"
                start_pct = 10 + int(70 * (i - 1) / total_dex)
                end_pct = 10 + int(70 * i / total_dex)
                await on_progress(start_pct, f"🧩 Decoding .dex to Smali... ({i}/{total_dex})")
                await run_baksmali(dex_path, out_dir, on_progress, start_pct, end_pct, i, total_dex)
                smali_dirs.append(out_dir)
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The .dex file is too big.", keep_button=False)
            return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "Smali Decode crashed")
            return

        if MODE == "extract":
            src_dir = await asyncio.to_thread(collect_com_folders, smali_dirs, work_dir)
            await on_progress(88, "📦 Extracting main package folders (com/)...")
        else:
            src_dir = work_dir / "smali_combined"
            src_dir.mkdir(exist_ok=True)
            for i, d in enumerate(smali_dirs, start=1):
                if total_dex == 1:
                    for item in Path(d).iterdir():
                        dst = src_dir / item.name
                        if item.is_dir():
                            shutil.copytree(item, dst, dirs_exist_ok=True)
                        else:
                            shutil.copy2(item, dst)
                else:
                    shutil.copytree(d, src_dir / f"dex_{i}")

        await on_progress(100, "✅ Smali Decode complete!")
        edit("📦 Packaging Smali Code...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "smali"
        suffix = "_com" if MODE == "extract" else ""
        zip_path = work_dir / f"{orig_stem}_smali{suffix}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(src_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    arcname = os.path.relpath(fp, src_dir)
                    zf.write(fp, arcname)

        if MODE == "extract":
            edit("✅ Smali Decode complete (com/ extracted)! Sending ZIP...")
        else:
            edit("✅ Smali Decode complete! Sending ZIP...")

        if MODE == "extract":
            caption = f"✅ Decoded <b>{safe_name}</b> → Smali (com/ folder extracted) — Powered By @R3V_X"
        else:
            caption = f"✅ Decoded <b>{safe_name}</b> to Smali Code — Powered By @R3V_X"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"✅ Decode complete!\n📤 Sending ZIP...\n\n{progress_bar(pct)}")

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
            edit("✅ Smali Decode complete! ZIP file delivered. 🔥", keep_button=False)

            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "Result upload failed")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
