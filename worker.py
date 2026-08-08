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


def proc_cpu_usage(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as f:
            parts = f.read().split()
        return int(parts[13]) + int(parts[14])
    except Exception:
        return -1


def detect_available_ram_gb() -> float:
    try:
        page = os.sysconf("SC_PAGE_SIZE")
        pages = os.sysconf("SC_PHYS_PAGES")
        if page and pages:
            return pages * page / (1024 ** 3)
    except Exception:
        pass
    return 0.0


def parse_mem_gb(value: str) -> int:
    m = re.match(r"(\d+(?:\.\d+)?)", value or "")
    return int(float(m.group(1))) if m else 4


def apply_memory_settings():
    requested = os.environ.get("JAVA_MAX_MEM", "4G")
    mem = requested
    avail = detect_available_ram_gb()
    if avail:
        # Reserve ~1 GB of headroom for Ghidra's native decompiler memory.
        cap = int(avail - 1)
        req = parse_mem_gb(requested)
        if cap >= 1 and req > cap:
            mem = f"{cap}G"
            log.warning("Capped JAVA_MAX_MEM %s -> %s (available RAM %.1f GB)", requested, mem, avail)
    props = GHIDRA_HOME / "support" / "launch.properties"
    try:
        text = props.read_text(errors="replace")
        # launch.properties ships JAVA_MAX_MEM commented out (e.g. "#JAVA_MAX_MEM=768M").
        # If we don't uncomment it, Ghidra silently runs with the tiny default heap and
        # big .so files die with java.lang.OutOfMemoryError during analysis.
        pattern = re.compile(r"^[ \t]*[#!]?[ \t]*JAVA_MAX_MEM\s*=.*$", flags=re.M)
        new = pattern.sub(f"JAVA_MAX_MEM={mem}", text)
        if new == text:
            new = text.rstrip("\n") + f"\nJAVA_MAX_MEM={mem}\n"
        if new != text:
            props.write_text(new)
        verify = props.read_text(errors="replace")
        m = re.search(r"^[ \t]*JAVA_MAX_MEM\s*=\s*(.+)$", verify, flags=re.M)
        log.info("JAVA_MAX_MEM -> %s (active line: %s)", mem, m.group(1).strip() if m else "NOT FOUND")
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


async def run_ghidra(file_path: Path, work_dir: Path, on_progress, disable_callfixup: bool = False) -> dict:
    project_dir = work_dir / "project"
    if project_dir.exists():
        # Crash-retry reuses the same work_dir; a stale project dir from the
        # previous attempt makes mkdir raise FileExistsError (Errno 17).
        shutil.rmtree(project_dir, ignore_errors=True)
    project_dir.mkdir(parents=True)
    out_c = work_dir / "decompiled.c"
    out_meta = work_dir / "info.txt"

    cmd = [
        str(ANALYZE_HEADLESS),
        str(project_dir),
        "Proj",
        "-overwrite",
    ]
    if disable_callfixup:
        props = work_dir / "analysis.options"
        props.write_text("Analysis.CallFixupAnalyzer.enabled=false\n")
        cmd.extend(["-properties", str(props)])
    cmd.extend([
        "-import", str(file_path),
        "-scriptPath", str(SCRIPT_DIR),
        "-postScript", "DecompileAll.java",
        str(out_c), str(out_meta),
        "-deleteProject",
    ])
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )

    tail = []
    await on_progress(5, "📥 Importing file into Ghidra...")

    async def read_stream():
        last_activity = time.monotonic()
        last_cpu = proc_cpu_usage(proc.pid)
        while True:
            if CANCELLED["v"]:
                proc.kill()
                raise JobCancelled()
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=60)
                last_activity = time.monotonic()
            except asyncio.TimeoutError:
                cpu = proc_cpu_usage(proc.pid)
                if cpu > last_cpu:
                    last_cpu = cpu
                    last_activity = time.monotonic()
                elif time.monotonic() - last_activity >= 1800:
                    proc.kill()
                    raise RuntimeError("Ghidra stalled: no CPU activity for 30 minutes")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            tail.append(line)
            del tail[:-250]
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
        rc = await asyncio.wait_for(read_stream(), timeout=18000)
    except asyncio.TimeoutError:
        proc.kill()
        raise TimeoutError("Ghidra analysis timed out")
    log.info("analyzeHeadless exit=%s", rc)
    tail_txt = "\n".join(tail[-50:])
    if rc != 0:
        raise RuntimeError(f"Ghidra exited with code {rc}:\n{extract_error_info(tail)[:1200]}")
    return {"c": out_c, "meta": out_meta, "tail": tail_txt, "lines": list(tail), "returncode": rc}


def extract_error_info(lines) -> str:
    keys = ("error", "exception", "failed", "unable", "cannot", "could not",
            "unsupported", "not recognized", "no language", "report", "caused by",
            "fatal", "import", "invalid", "unknown")
    out = []
    for ln in (lines or []):
        if any(k in ln.lower() for k in keys):
            out.append(ln)
    return "\n".join(out[-16:]) if out else "\n".join((lines or [])[-40:])


def send_document(file_path: Path, caption: str, filename: str):
    with open(file_path, "rb") as fh:
        resp = httpx.post(
            f"{API}/sendDocument",
            data={"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"document": (filename, fh, "application/zip")},
            timeout=180,
        )
    return resp.json()


def check_zip_limits(file_path: Path):
    if IS_ADMIN:
        return
    if Path(FILENAME).suffix.lower() != ".zip":
        return
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
    so_dex = sum(1 for n in names if n.lower().endswith((".so", ".dex")))
    apks = sum(1 for n in names if n.lower().endswith(".apk"))
    max_so_dex = 5 if IS_PREMIUM else 1
    max_apk = 2 if IS_PREMIUM else 1
    if so_dex > max_so_dex:
        raise ValueError(f"ZIP contains {so_dex} .so/.dex files — max {max_so_dex} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")
    if apks > max_apk:
        raise ValueError(f"ZIP contains {apks} .apk files — max {max_apk} allowed for {'Premium' if IS_PREMIUM else 'Free'} users.")


def count_zip_so_dex(file_path: Path) -> int:
    if Path(FILENAME).suffix.lower() != ".zip":
        return 0
    import zipfile
    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
    return sum(1 for n in names if n.lower().endswith((".so", ".dex")))


def report_extra_count(extra: int):
    if not REPORT_URL or not REPORT_TOKEN or extra <= 0:
        return
    try:
        httpx.post(
            REPORT_URL,
            json={"user_id": USER_ID, "count": extra},
            headers={"X-Count-Token": REPORT_TOKEN},
            timeout=10,
        )
        log.info("Reported extra count %d for user %s", extra, USER_ID)
    except Exception as e:
        log.warning("Count report failed: %s", e)


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

        start_t = time.monotonic()
        last = [0, "", 0.0]

        async def on_progress(pct: int, label: str = "🧠 Analyzing..."):
            now = time.monotonic()
            if pct - last[0] < 5 and label == last[1] and now - last[2] < 60:
                return
            last[0], last[1], last[2] = pct, label, now
            mins = int((now - start_t) // 60)
            elapsed = f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m {int(now - start_t) % 60:02d}s"
            edit(f"{label}\n{progress_bar(pct)}\n⏱ {elapsed}")

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
                                run_ghidra(bin_path, work_dir / f"analysis_{idx}", on_progress, disable_callfixup=(attempt == 2)), timeout=18000
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
                            run_ghidra(dest, work_dir / "analysis", on_progress, disable_callfixup=(attempt == 2)), timeout=18000
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
