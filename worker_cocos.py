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
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 100

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





def tg(method: str, **params):
    try:
        resp = httpx.post(f"{API}/{method}", data=params, timeout=60)
        return resp.json()
    except Exception as e:
        log.warning("tg %s failed: %s", method, e)
        return None


import json

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


async def send_error_log(work_dir, exception_obj, title="Processing failed"):
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


ENGINE = os.environ.get("PAYLOAD_ENGINE", "cocos-both")

async def run_cocos_build(src_zip: Path, work_dir: Path, on_progress) -> Path:
    src_dir = work_dir / "src"
    out_dir = work_dir / "out"
    src_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)
    
    with zipfile.ZipFile(src_zip, 'r') as zf:
        zf.extractall(src_dir)
        
    await on_progress(50, "🛠️ Extracted source. Preparing Cocos compiler...")
    
    arch = ENGINE.split("-")[1] if "-" in ENGINE else "both"
    if arch == "32":
        compiler_arch = "32-bit"
    elif arch == "64":
        compiler_arch = "64-bit"
    else:
        compiler_arch = "both"
        
    await on_progress(60, f"🛠️ Compiling JS/Lua to bytecode ({compiler_arch})...")
    
    # We will iterate through files and compile them
    js_files = list(src_dir.rglob("*.js"))
    lua_files = list(src_dir.rglob("*.lua"))
    
    total_files = len(js_files) + len(lua_files)
    if total_files == 0:
        raise RuntimeError("No .js or .lua files found in the uploaded ZIP!")
        
    for i, fp in enumerate(js_files):
        rel = fp.relative_to(src_dir)
        out_fp = out_dir / rel.with_suffix(".jsc")
        out_fp.parent.mkdir(parents=True, exist_ok=True)
        
        # Here we should call the actual jscompile binary for the selected architecture.
        # For now, we will simulate or use a dummy command until the user provides the specific jsc binaries.
        # cocos jscompile -s src -d dst
        
        # NOTE: To implement the REAL Cocos JS compilation, we need the Spidermonkey binaries for 32/64 bit.
        # We will attempt to run a generic command. If it fails, the error handler will catch it and send error.txt
        proc = await asyncio.create_subprocess_exec(
            "jscompile", "--arch", arch, str(fp), "-o", str(out_fp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"jscompile failed! (Ensure jscompile is installed for {arch})\n")
            
        if i % 5 == 0:
            await on_progress(60 + int((i/total_files)*30), f"⚙️ Compiling JS: {rel.name}...")
            
    for i, fp in enumerate(lua_files):
        rel = fp.relative_to(src_dir)
        out_fp = out_dir / rel.with_suffix(".luac")
        out_fp.parent.mkdir(parents=True, exist_ok=True)
        
        proc = await asyncio.create_subprocess_exec(
            "luajit", "-b", str(fp), str(out_fp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.wait()
        if proc.returncode != 0:
            raise RuntimeError("luajit failed to compile Lua script!")
            
        await on_progress(60 + int(((len(js_files)+i)/total_files)*30), f"⚙️ Compiling Lua: {rel.name}...")

    await on_progress(90, "📦 Build complete. Packaging bytecode...")
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

    edit("🟢 Job started! Preparing Cocos compilation on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("cocos_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        dest = work_dir / "input.zip"
        last = [-100.0]

        async def on_dl(pct: float):
            if pct < last[0] or (pct - last[0] < 2.0 and pct < 100.0): return
            last[0] = pct
            is_mtproto = bool(os.environ.get("PAYLOAD_FILE_ID", ""))
            dl_text = "📥 Downloading ZIP via MTProto (Pyrogram)..." if is_mtproto else "📥 Downloading ZIP..."
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
                async for raw in proc.stdout:
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
                filename = FILENAME or "download.zip"
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
                filename_dl = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Starting Cocos compilation...")

        last_prog = [0, ""]
        async def on_progress(pct: int, text: str):
            if pct < last_prog[0] or (pct == last_prog[0] and text == last_prog[1]): return
            last_prog[0] = pct; last_prog[1] = text
            edit(f"{text}\n\n{progress_bar(pct)}")

        try:
            out_dir = await run_cocos_build(dest, work_dir, on_progress)
            bname = Path(FILENAME).stem or "source"
        except asyncio.TimeoutError:
            edit("⏰ Cocos build timeout.", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "Cocos Build crashed")
            return

        edit("📦 Packaging compiled scripts...")
        safe_name = re.sub(r'[^A-Za-z0-9._-]+', "_", FILENAME)[:60] or "source"
        orig_stem = Path(safe_name).stem or "compiled"
        zip_path = work_dir / f"{orig_stem}_cocos_scripts.zip"
        
        has_files = False
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(out_dir):
                for f in files:
                    fp = Path(root) / f
                    arcname = fp.relative_to(out_dir)
                    zf.write(fp, arcname)
                    has_files = True
                    
        if not has_files:
            edit("❌ Build succeeded but no bytecode files were generated.", keep_button=False)
            return

        edit("✅ Compilation complete! Sending ZIP...")
        caption = f"✅ Cocos Compiled <b>{safe_name}</b>\nArchitecture: <code>{ENGINE.split('-')[1]}</code>\n— Powered By @Ghostofhackers & @R3V_X"
        up_last = [0]
        async def on_up(pct: int):
            if pct < up_last[0] or pct - up_last[0] < 2: return
            up_last[0] = pct
            edit(f"✅ Decompilation complete!\n📤 Sending ZIP...\n\n{progress_bar(pct)}")

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", str(zip_path), caption,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            async for raw in proc.stdout:
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
            edit("✅ Decompilation complete! ZIP file delivered via MTProto. 🔥", keep_button=False)
            
            if JOB_ID:
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
        except Exception as e:
            await send_error_log(work_dir, e, "MTProto upload failed")

    except Exception as fatal_e:
        if 'work_dir' in locals():
            await send_error_log(work_dir, fatal_e, "Fatal Worker Crash")
        else:
            edit("❌ Fatal Crash before initialization.", keep_button=False)
    finally:
        if 'work_dir' in locals():
            shutil.rmtree(work_dir, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
