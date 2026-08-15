import asyncio
import json
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
log = logging.getLogger("worker_pdf_txt")

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
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else (300 if IS_PREMIUM else 30)

MAX_PDF_FREE = 5
MAX_PDF_PREMIUM = 20

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


class TolerantZipFile(zipfile.ZipFile):
    @property
    def _end_offset(self):
        return None


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

def upload_result_for_app(file_to_send: Path):
    if not JOB_ID or not file_to_send or not file_to_send.exists():
        return

    target_chat = CHAT_ID if CHAT_ID and CHAT_ID != '0' and not str(CHAT_ID).startswith('app_') else 'me'

    api_id_val = os.environ.get('API_ID', '').strip()
    api_hash_val = os.environ.get('API_HASH', '').strip()
    bot_tok = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    
    if api_id_val and api_hash_val:
        try:
            import subprocess
            cmd = [sys.executable, 'upload_file.py', str(file_to_send), f'✅ Result for Job {JOB_ID}', str(target_chat)]
            proc = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
            if proc.returncode == 0:
                log.info('Uploaded to Telegram MTProto successfully: %s', proc.stdout)
                for line in proc.stdout.splitlines():
                    if line.startswith('UPLOAD_SUCCESS:'):
                        parts = line.split(':')
                        if len(parts) >= 2:
                            bot_username = parts[1]
                            notify_app(f'FINAL_ZIP_URL:telegram_deeplink:{bot_username}')
                            return
                notify_app('FINAL_ZIP_URL:telegram_direct_upload')
                return
            else:
                log.warning('MTProto upload failed with code %d: %s', proc.returncode, proc.stderr)
        except Exception as e:
            log.warning('MTProto upload in upload_result_for_app failed: %s', e)

    notify_app('FINAL_ZIP_URL:telegram_direct_upload')


def tg(method: str, **params):
    try:
        resp = httpx.post(f"{API}/{method}", data=params, timeout=60)
        return resp.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return None


def edit(text: str, parse_mode: str = None, keep_button: bool = True):
    if CANCELLED["v"]:
        return
    notify_app(text)
    if not BOT_TOKEN or not CHAT_ID or BOT_TOKEN == "app_direct_mode":
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


def proc_cpu_usage(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return -1


async def send_error_log(work_dir, exception_obj, title="PDF → TXT conversion failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    sent = False
    try:
        err_file = Path(work_dir) / "error.txt"
        err_file.write_text(f"❌ {title}:\n\n{err_str}")
        caption = f"❌ Error Log:\n{str(exception_obj)[:100]}"
        try:
            with open(err_file, "rb") as ef:
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(url, data={"chat_id": CHAT_ID, "caption": caption}, files={"document": ef})
                    resp.raise_for_status()
            sent = True
        except Exception as e:
            log.warning("HTTP error log upload failed, falling back to MTProto: %s", e)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", str(err_file), caption,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            await proc.wait()
            sent = (proc.returncode == 0)
    except Exception as e:
        log.error("Failed to upload error log: %s", e)
    if sent:
        edit(f"❌ {title}. Error log sent.", keep_button=False)
    else:
        edit(f"❌ {title}. Could not send error log. Try again later.", keep_button=False)


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

                filename = "download"
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
                        if CANCELLED["v"]:
                            raise JobCancelled()
                        fh.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = min(100, int(downloaded * 100 / total))
                            await on_progress(pct)
                return filename
        raise ValueError("Could not download file from this link.")


async def run_tool(cmd: list, on_progress, label: str, timeout: int = 86400, progress_stall: int = 1800, heartbeat=None, check_errors: bool = True):
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out_lines = []
    last_hb = time.monotonic()

    async def read_stream():
        nonlocal last_hb
        last_activity = time.monotonic()
        last_cpu = proc_cpu_usage(proc.pid)
        while True:
            if CANCELLED["v"]:
                proc.kill()
                raise JobCancelled()
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                last_activity = time.monotonic()
                last_hb = time.monotonic()
            except asyncio.TimeoutError:
                cpu = proc_cpu_usage(proc.pid)
                if cpu > last_cpu:
                    last_cpu = cpu
                    last_activity = time.monotonic()
                elif time.monotonic() - last_activity >= progress_stall:
                    proc.kill()
                    raise RuntimeError(f"{label} stalled: no CPU activity for {progress_stall//60} minutes")
                if heartbeat and time.monotonic() - last_hb >= 60:
                    last_hb = time.monotonic()
                    await heartbeat()
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
                if len(out_lines) > 100:
                    del out_lines[:-100]
                if check_errors and ("error" in line.lower() or "exception" in line.lower()):
                    await on_progress(60, f"⚠️ {label} (checking)...")
        return await proc.wait()

    try:
        rc = await asyncio.wait_for(read_stream(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError(f"{label} timed out")

    if rc != 0:
        raise RuntimeError(f"{label} failed with exit code {rc}:\n" + "\n".join(out_lines[-25:]))
    return "\n".join(out_lines[-25:])


def find_inputs(src_dir: Path, suffixes) -> list:
    return [p for p in sorted(Path(src_dir).rglob("*")) if p.is_file() and p.suffix.lower() in suffixes]


def is_zip_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"PK\x03\x04"
    except Exception:
        return False


def is_pdf_file(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except Exception:
        return False


OCR_LANGS = "eng+hin+urd+ben+mar+guj+tam+tel+kan+mal+pan"
OCR_PSMS = ("3", "6", "11")


def has_meaningful_text(data: str) -> bool:
    return any(ch.isalnum() for ch in data.replace("\x0c", " ").strip())


async def convert_single_pdf(pdf_path: Path, txt_path: Path, on_progress, label: str) -> bool:
    """Extract text with progressive flag fallback. Returns True if meaningful output produced."""
    if txt_path.exists():
        txt_path.unlink()
    started = time.monotonic()

    async def hb():
        mins = int((time.monotonic() - started) // 60)
        edit(f"⏳ {label}: still converting... ({mins} min)", keep_button=True)

    candidates = [
        ["pdftotext", "-q", "-layout", str(pdf_path), str(txt_path)],
        ["pdftotext", "-q", "-raw", str(pdf_path), str(txt_path)],
        ["pdftotext", "-q", "-fixed", "10", str(pdf_path), str(txt_path)],
        ["pdftotext", "-q", str(pdf_path), str(txt_path)],
    ]
    for cmd in candidates:
        try:
            await run_tool(cmd, on_progress, label, timeout=1200, progress_stall=600, heartbeat=hb, check_errors=False)
        except Exception as e:
            log.warning("%s attempt failed: %s", label, e)
            if txt_path.exists():
                txt_path.unlink()
            continue
        if txt_path.exists():
            content = txt_path.read_text(encoding="utf-8", errors="replace")
            if has_meaningful_text(content):
                return True
            txt_path.unlink()
    return False


async def ocr_page(page: Path, base: Path, on_progress, label: str) -> str:
    for psm in OCR_PSMS:
        out = Path(str(base) + f"_psm{psm}.txt")
        if out.exists():
            out.unlink()
        try:
            await run_tool(["tesseract", str(page), str(Path(str(base) + f"_psm{psm}")), "-l", OCR_LANGS, "--psm", psm],
                           on_progress, label, timeout=300, progress_stall=180, check_errors=False)
        except Exception as e:
            log.warning("tesseract failed for %s: %s", page.name, e)
            continue
        if out.exists():
            content = out.read_text(encoding="utf-8", errors="replace")
            if has_meaningful_text(content):
                return content
    return ""


async def ocr_pdf(pdf_path: Path, txt_path: Path, on_progress, label: str, start_pct: int = 0, end_pct: int = 100) -> bool:
    """OCR fallback for scanned (image-only) PDFs via pdftoppm + tesseract."""
    if not shutil.which("pdftoppm") or not shutil.which("tesseract"):
        log.warning("OCR tools missing (pdftoppm/tesseract)")
        return False
    await on_progress(start_pct, f"🔍 {label}: rendering pages...")
    ocr_dir = pdf_path.parent / (pdf_path.stem + "_ocr")
    ocr_dir.mkdir(exist_ok=True)
    try:
        await run_tool(["pdftoppm", "-r", "300", "-jpeg", str(pdf_path), str(ocr_dir / "pg")], on_progress, label, timeout=1200, progress_stall=600, check_errors=False)
    except Exception as e:
        log.warning("pdftoppm failed for %s: %s", pdf_path.name, e)
        return False
    pages = [p for p in ocr_dir.iterdir() if p.suffix.lower() == ".jpg"]
    pages.sort(key=lambda p: int(re.search(r"(\d+)\.jpg$", p.name).group(1)) if re.search(r"(\d+)\.jpg$", p.name) else 0)
    if not pages:
        log.warning("No pages rendered for %s", pdf_path.name)
        return False
    texts = []
    total_pages = len(pages)
    span = max(1, end_pct - start_pct)
    for i, page in enumerate(pages, 1):
        if CANCELLED["v"]:
            raise JobCancelled()
        pct = start_pct + int((i - 1) * span / total_pages)
        await on_progress(pct, f"🔍 {label}: OCR page {i}/{total_pages}...")
        t = await ocr_page(page, ocr_dir / f"out_{i}", on_progress, label)
        if t:
            texts.append(t)
    if not texts:
        log.warning("OCR produced no text for %s", pdf_path.name)
        return False
    await on_progress(end_pct - 1, f"🔍 {label}: saving text...")
    txt_path.write_text("\n".join(texts), encoding="utf-8")
    return has_meaningful_text(txt_path.read_text(encoding="utf-8", errors="replace"))


async def convert_pdfs(input_path: Path, work_dir: Path, on_progress) -> tuple:
    await on_progress(20, "📄 Preparing PDF files...")
    pdf_files: list = []
    is_single = False
    ext = Path(FILENAME).suffix.lower()

    if input_path.is_file():
        if ext == ".pdf" or is_pdf_file(input_path):
            pdf_files = [input_path]
            is_single = True
        elif ext == ".zip" or is_zip_file(input_path):
            extract_dir = work_dir / "pdf_src"
            extract_dir.mkdir(exist_ok=True)
            with TolerantZipFile(input_path, "r") as zf:
                zf.extractall(extract_dir)
            pdf_files = find_inputs(extract_dir, {".pdf"})
        else:
            raise ValueError("Unsupported input. Send a .pdf file or a ZIP containing PDF files.")
    elif input_path.is_dir():
        pdf_files = find_inputs(input_path, {".pdf"})
    else:
        raise ValueError("Unsupported input for PDF conversion.")

    if not pdf_files:
        raise ValueError("No PDF files found in the input.")

    max_pdf = MAX_PDF_PREMIUM if IS_PREMIUM else MAX_PDF_FREE
    if not IS_ADMIN and len(pdf_files) > max_pdf:
        raise ValueError(f"Too many PDFs in ZIP: {len(pdf_files)} — max {max_pdf} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")

    scanned = []
    ocr_used = 0
    total = len(pdf_files)
    for i, pf in enumerate(pdf_files, 1):
        if CANCELLED["v"]:
            raise JobCancelled()
        txt = pf.with_suffix(".txt")
        band_start = int(20 + (i - 1) * 60 / total)
        band_end = int(20 + i * 60 / total)
        await on_progress(band_start, f"📄 Converting PDF {i}/{total}...")
        ok = await convert_single_pdf(pf, txt, on_progress, f"pdftotext {i}")
        if not ok:
            ok = await ocr_pdf(pf, txt, on_progress, f"OCR {i}", band_start, band_end)
            if ok:
                ocr_used += 1
        if not ok:
            scanned.append(pf.name)

    await on_progress(85, "📦 Packaging TXT files...")
    if is_single:
        result = Path(pdf_files[0]).with_suffix(".txt")
        if not result.exists():
            raise ValueError("pdftotext produced no output file.")
    else:
        result = work_dir / "txt_files.zip"
        with TolerantZipFile(result, "w", zipfile.ZIP_DEFLATED) as zf:
            for pf in pdf_files:
                txt = Path(pf).with_suffix(".txt")
                if txt.exists():
                    zf.write(txt, txt.name)
        if not os.path.getsize(result):
            raise ValueError("No TXT output produced.")
    return result, scanned, ocr_used


async def main():
    if not JOB_ID and (not BOT_TOKEN or not CHAT_ID):
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing PDF → TXT engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("pdftxt_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        ext = Path(FILENAME).suffix or ".pdf"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        dl_method = ["📥 Downloading file..."]
        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            edit(f"{dl_method[0]}\n\n{progress_bar(pct)}")

        try:
            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            tg_file_path = TG_FILE_PATH
            got_file = False
            
            if file_id and not tg_file_path:
                try:
                    async with httpx.AsyncClient(timeout=30) as client:
                        gf = await client.get(f"{API}/getFile?file_id={file_id}")
                        if gf.status_code == 200:
                            g_data = gf.json()
                            if g_data.get("ok"):
                                tg_file_path = g_data["result"].get("file_path", "")
                except Exception as e:
                    log.warning("getFile error: %s", e)

            if tg_file_path:
                try:
                    filename = FILENAME or "download"
                    file_api = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
                    tg_url = tg_file_path if tg_file_path.startswith("http") else f"{file_api}/{tg_file_path}"
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
                filename = FILENAME or "download"
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing PDF conversion...")

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "📄 Converting PDF → TXT..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            result, scanned, ocr_used = await convert_pdfs(dest, work_dir, on_progress)
            notes = []
            if ocr_used:
                notes.append(f"🔍 OCR used on {ocr_used} PDF(s)")
            if scanned:
                notes.append(f"⚠️ {len(scanned)} PDF(s) had no readable text")
            note_str = " | ".join(notes)
            caption = f"✅ PDF converted → <b>TXT</b>" + (f" ({note_str})" if note_str else "") + " — Powered By @R3V_X"
            done_msg = "✅ PDF → TXT conversion complete!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The PDF is too large to convert.", keep_button=False)
            return
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "PDF → TXT conversion crashed")
            return

        await on_progress(100, done_msg)
        edit("📦 Packaging TXT files...")

        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"{done_msg}\n📤 Sending TXT...\n\n{progress_bar(pct)}")

        if JOB_ID:
            upload_result_for_app(result)

        elif BOT_TOKEN and BOT_TOKEN != "app_direct_mode" and CHAT_ID:
            try:
                http_ok = False
                MAX_HTTP_UPLOAD = 50 * 1024 * 1024
                if result.stat().st_size <= MAX_HTTP_UPLOAD:
                    try:
                        with open(result, "rb") as doc_f:
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
                    if os.environ.get("API_ID", "").strip() and os.environ.get("API_HASH", "").strip():
                        proc = await asyncio.create_subprocess_exec(
                            sys.executable, "upload_file.py", str(result), caption,
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
                            log.warning("MTProto upload failed with exit code %d", proc.returncode)
            except Exception as e:
                log.warning("Telegram upload failed: %s", e)
                if not JOB_ID:
                    edit(f"❌ Result ready, but Telegram upload failed: {e}", keep_button=False)

        edit("✅ Process complete! Result delivered. 🔥", keep_button=False)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
