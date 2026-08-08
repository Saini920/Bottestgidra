import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
IS_PREMIUM = os.environ.get("PAYLOAD_IS_PREMIUM", "False").lower() == "true"
USER_ID = os.environ.get("PAYLOAD_USER_ID", CHAT_ID)
REPORT_URL = os.environ.get("PAYLOAD_REPORT_URL", "")
REPORT_TOKEN = BOT_TOKEN
GHIDRA_HOME = Path(os.environ.get("GHIDRA_HOME", "/opt/ghidra"))
ANALYZE_HEADLESS = GHIDRA_HOME / "support" / "analyzeHeadless"
SCRIPT_DIR = Path(__file__).resolve().parent / "ghidra_scripts"
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


async def main():
    if not BOT_TOKEN or not CHAT_ID:
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)
    edit("🟢 Job started! Preparing Ghidra engine on cloud server...", parse_mode="HTML")
    apply_memory_settings()

    work_dir = Path(tempfile.gettempdir()) / ("ghidra_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        in_ext = Path(FILENAME or "input.bin").suffix.lower()
        if not in_ext or not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", in_ext):
            in_ext = ".bin"
        dest = work_dir / ("input" + in_ext)
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
                    filename = FILENAME or "download.bin"
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
                filename = FILENAME or "download.bin"
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
            edit("❌ Download failed: " + str(e)[:300])
            return

        size = dest.stat().st_size
        if size == 0:
            edit("❌ Downloaded file is empty.", keep_button=False)
            return
        if size > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.", keep_button=False)
            return

        file_magic = ""
        try:
            with open(dest, "rb") as fh:
                file_magic = fh.read(16).hex(" ")
            log.info("Downloaded %s bytes, magic: %s", size, file_magic)
        except Exception as e:
            log.warning("Could not read magic: %s", e)

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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Ghidra analysis...")

        last = [0, ""]

        async def on_progress(pct: int, label: str = "🧠 Analyzing..."):
            if pct - last[0] < 5 and label == last[1]:
                return
            last[0], last[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        out_files = []

        # Check if downloaded file is a ZIP archive containing multiple binaries (Batch Decompile)
        if zipfile.is_zipfile(dest):
            extract_dir = work_dir / "extracted_batch"
            extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                with zipfile.ZipFile(dest, "r") as zf:
                    zf.extractall(extract_dir)
            except Exception as e:
                log.warning("ZIP extract error: %s", e)

            candidates = []
            is_apk = filename.lower().endswith(".apk")
            for root, dirs, files in os.walk(extract_dir):
                for f in files:
                    fp = Path(root) / f
                    ext = fp.suffix.lower()
                    if is_apk:
                        # For APKs, only decompile native .so files
                        if ext == ".so":
                            candidates.append(fp)
                    else:
                        if ext in [".so", ".dll", ".exe", ".elf", ".apk", ".bin", ".jar", ".o", ".dylib"] or (not ext and fp.stat().st_size > 1024):
                            candidates.append(fp)

            if len(candidates) > 5 and not IS_ADMIN:
                edit(f"⚠️ <b>Batch Limit Exceeded!</b>\nArchive contains <b>{len(candidates)} binary files</b>. Maximum batch limit is <b>5 files</b> per ZIP.", parse_mode="HTML")
                return

            if len(candidates) >= 1:
                edit(f"📦 <b>Batch / APK Detected!</b> Found {len(candidates)} binary file(s). Starting multi-file decompilation...", parse_mode="HTML")
                for idx, bin_path in enumerate(candidates, start=1):
                    edit(f"⚙️ <b>Processing ({idx}/{len(candidates)}):</b> <code>{bin_path.name}</code>...", parse_mode="HTML")
                    for attempt in (1, 2):
                        try:
                            res = await asyncio.wait_for(
                                run_ghidra(bin_path, work_dir / f"analysis_{idx}", on_progress, disable_callfixup=(attempt == 2)), timeout=3600
                            )
                            bname = bin_path.stem
                            if res["c"].exists() and res["c"].stat().st_size > 0:
                                out_files.append((f"{bname}.c", res["c"]))
                            if res["meta"].exists() and res["meta"].stat().st_size > 0:
                                out_files.append((f"{bname}_info.txt", res["meta"]))
                            break
                        except RuntimeError as e:
                            if attempt == 1:
                                log.warning("Batch file %s crashed, retrying with CallFixupAnalyzer disabled: %s", bin_path.name, e)
                                continue
                            log.warning("Batch file %s failed: %s", bin_path.name, e)
                            break
                        except Exception as e:
                            log.warning("Batch file %s failed: %s", bin_path.name, e)
                            break

        if not out_files:
            try:
                result = None
                for attempt in (1, 2):
                    try:
                        result = await asyncio.wait_for(
                            run_ghidra(dest, work_dir / "analysis", on_progress, disable_callfixup=(attempt == 2)), timeout=7200
                        )
                        break
                    except RuntimeError as e:
                        if attempt == 1:
                            log.warning("Ghidra crashed, retrying with CallFixupAnalyzer disabled: %s", e)
                            continue
                        raise
                bname = Path(filename).stem or "decompiled"
                if result["c"].exists() and result["c"].stat().st_size > 0:
                    out_files.append((f"{bname}.c", result["c"]))
                if result["meta"].exists() and result["meta"].stat().st_size > 0:
                    out_files.append((f"{bname}_info.txt", result["meta"]))
            except TimeoutError:
                edit("⏰ Timeout! The file is too big or complex.", keep_button=False)
                return
            except Exception as e:
                log.exception("Ghidra crashed")
                err = str(e)[:1200].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                edit("❌ Decompilation failed: <code>" + err + "</code>", keep_button=False)
                return

        if not out_files:
            diag = ""
            try:
                diag = extract_error_info(result.get("lines"))[:1400]
            except Exception:
                diag = ""
            if not diag:
                try:
                    diag = result["tail"][-400:]
                except Exception:
                    diag = ""
            msg = "❌ Analysis failed or no output files generated."
            if file_magic:
                msg += f"\n\n📦 File: {size/1024/1024:.1f} MB · magic: <code>{file_magic}</code>"
            if diag:
                msg += "\n\n<code>" + diag.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</code>"
            edit(msg, parse_mode="HTML")
            return

        edit("📦 Packaging results...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", filename)[:60] or "file"
        orig_stem = Path(safe_name).stem or "decompiled"

        zip_path = work_dir / f"{orig_stem}_decompiled.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for arcname, fp in out_files:
                zf.write(fp, arcname)

        edit("✅ Decompilation complete! Sending ZIP...")
        
        caption = f"✅ Decompiled <b>{safe_name}</b> with Ghidra — Powered By @R3V_X"
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
            edit("✅ Decompilation complete! ZIP file delivered. 🔥", keep_button=False)
            
            if JOB_ID:
                # App integration needs a direct link which we no longer have. 
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
