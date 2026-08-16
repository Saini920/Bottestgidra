import asyncio
import base64
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
log = logging.getLogger("worker_apk_sign")

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
SDK_ROOT = os.environ.get("PAYLOAD_SDK_ROOT", "")
MIN_SDK = os.environ.get("PAYLOAD_MIN_SDK", "")
MAX_SDK = os.environ.get("PAYLOAD_MAX_SDK", "")
KEYSTORE_JSON = os.environ.get("PAYLOAD_CUSTOM_KEYSTORE_JSON") or os.environ.get("PAYLOAD_KEYSTORE", "")
CUSTOM_KEY_ERROR = ""  # human-readable reason when the custom keystore cannot be used
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

CANCELLED = {"v": False}


class JobCancelled(BaseException):
    pass


TOOL_LOG_FH = None


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


async def send_error_log(work_dir, exception_obj, title="APK Sign failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    sent = False
    try:
        err_file = Path(work_dir) / "error.txt"
        text = f"❌ {title}:\n\n{err_str}"
        if TOOL_LOG_FH is not None:
            try:
                TOOL_LOG_FH.flush()
                log_path = Path(TOOL_LOG_FH.name)
                if log_path.exists():
                    tail = "\n".join(log_path.read_text(errors="replace").splitlines()[-500:])
                    text += f"\n\n══════════ TOOL OUTPUT (last 500 lines) ══════════\n{tail}"
            except Exception:
                pass
        err_file.write_text(text)
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


async def run_tool(cmd: list, on_progress, label: str, timeout: int = 86400, progress_stall: int = 1800):
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out_lines = []

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
                elif time.monotonic() - last_activity >= progress_stall:
                    proc.kill()
                    raise RuntimeError(f"{label} stalled: no CPU activity for {progress_stall//60} minutes")
                continue
            if not raw:
                break
            line = raw.decode(errors="replace").strip()
            if line:
                out_lines.append(line)
                if len(out_lines) > 100:
                    del out_lines[:-100]
                if TOOL_LOG_FH is not None:
                    try:
                        TOOL_LOG_FH.write(line + "\n")
                    except Exception:
                        pass
                if "error" in line.lower() or "exception" in line.lower():
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


def find_sdk():
    root = SDK_ROOT or os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk"
    if not os.path.isdir(root):
        home_sdk = Path.home() / "Android" / "Sdk"
        if home_sdk.is_dir():
            root = str(home_sdk)
    bt_dir = os.path.join(root, "build-tools")
    build_tools = []
    if os.path.isdir(bt_dir):
        build_tools = sorted(
            (d for d in os.listdir(bt_dir) if os.path.isdir(os.path.join(bt_dir, d))),
            key=lambda v: [int(x) for x in re.findall(r"\d+", v)] or [0], reverse=True)
    return {"root": root, "build_tools": build_tools}


def get_tool(sdk, name):
    for v in sdk["build_tools"]:
        p = os.path.join(sdk["root"], "build-tools", v, name)
        if os.path.exists(p):
            return p
    return ""


class TolerantZipFile(zipfile.ZipFile):
    def _RealGetContents(self):
        super()._RealGetContents()
        for zinfo in self.filelist:
            zinfo._end_offset = None


def strip_old_signatures(input_apk: Path, out_apk: Path):
    drop = re.compile(r"^META-INF/.*\.(RSA|DSA|EC|SF)$", re.IGNORECASE)
    try:
        with TolerantZipFile(input_apk) as zin:
            with zipfile.ZipFile(out_apk, "w") as zout:
                for item in zin.infolist():
                    if drop.match(item.filename) or item.filename.upper() == "META-INF/MANIFEST.MF":
                        continue
                    compress_type = item.compress_type
                    if item.filename.endswith(".so") or item.filename == "resources.arsc":
                        compress_type = zipfile.ZIP_STORED
                    zout.writestr(item, zin.read(item.filename), compress_type=compress_type)
    except Exception as e:
        import shutil
        shutil.copy2(input_apk, out_apk)


def make_keystore(path: Path) -> Path:
    if not path.exists():
        subprocess.run(["keytool", "-genkeypair", "-keystore", str(path), "-storepass", "android", "-keypass", "android",
                        "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
                        "-dname", "CN=Android Debug,O=Android,C=US"], check=True, capture_output=True)
    return path


def inspect_custom_keystore(ks_path: Path, storepass: str):
    try:
        proc = subprocess.run(
            ["keytool", "-list", "-v", "-keystore", str(ks_path), "-storepass", storepass],
            capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout + proc.stderr
    except Exception as e:
        log.warning("keytool inspect failed: %s", e)
        return "", [], True

    m = re.search(r"Keystore type:\s*([A-Za-z0-9_]+)", out, re.I)
    ks_type = m.group(1).lower() if m else ""
    
    aliases = []
    for line in out.splitlines():
        line_s = line.strip()
        m_alias = re.search(r"Alias name:\s*(.+)", line_s, re.I)
        if m_alias:
            a = m_alias.group(1).strip()
            if a and a not in aliases:
                aliases.append(a)
        elif "," in line_s and any(k in line_s.lower() for k in ["privatekey", "keyentry", "entry"]):
            a = line_s.split(",")[0].strip()
            if a and a not in aliases:
                aliases.append(a)

    if proc.returncode != 0:
        if "password" in out.lower() or "tampered" in out.lower() or "integrity" in out.lower():
            return "", [], False
        return ks_type, aliases, True
    return ks_type, aliases, True


def get_custom_keystore(work_dir: Path):
    """Return (keystore_path, storepass, keypass, alias, ks_type) or None.

    Never raises: on any problem it records a human-readable reason in the
    module-level CUSTOM_KEY_ERROR (used to tell the user why their custom key
    was not applied) and returns None, so signing falls back to the debug key.
    """
    global CUSTOM_KEY_ERROR
    CUSTOM_KEY_ERROR = ""
    if not KEYSTORE_JSON or not KEYSTORE_JSON.strip():
        return None
    try:
        info = json.loads(KEYSTORE_JSON)
        b64 = (info.get("keystore_b64") or info.get("b64") or "").strip()
        if not b64:
            CUSTOM_KEY_ERROR = "Keystore file data is missing from the job payload."
            return None
        try:
            ks_bytes = base64.b64decode(b64)
        except Exception:
            CUSTOM_KEY_ERROR = "Keystore file data is corrupt (invalid base64). Re-upload the keystore file."
            return None
        if not ks_bytes:
            CUSTOM_KEY_ERROR = "Keystore file is empty."
            return None
        ks_path = work_dir / "custom.keystore"
        ks_path.write_bytes(ks_bytes)
        storepass = (info.get("storepass") or "").strip() or "android"
        keypass = (info.get("keypass") or "").strip() or storepass
        alias = (info.get("alias") or "").strip()
        ks_type, aliases, ok = inspect_custom_keystore(ks_path, storepass)
        if not ok:
            CUSTOM_KEY_ERROR = (
                "Custom signing key password (storepass) is incorrect, or the keystore file is damaged. "
                "Re-upload the keystore and re-enter the correct password in Settings (or /setkey)."
            )
            return None
        if alias and aliases and alias not in aliases:
            alias = aliases[0]
        if not alias and aliases:
            alias = aliases[0]
        if not alias:
            CUSTOM_KEY_ERROR = "Could not detect any alias inside the keystore."
            return None
        return ks_path, storepass, keypass, alias, ks_type
    except Exception as e:
        log.warning("Failed to parse custom keystore: %s", e)
        CUSTOM_KEY_ERROR = f"Failed to read the custom keystore: {e}"
        return None


async def sign_apk(input_apk: Path, work_dir: Path, on_progress, sdk) -> Path:
    await on_progress(20, "🔏 Validating APK...")
    try:
        with TolerantZipFile(input_apk) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError) as e:
        raise ValueError("File is not a valid APK (ZIP archive).")
    if not any(n.lower() == "androidmanifest.xml" for n in names):
        raise ValueError("Not a valid APK (AndroidManifest.xml missing).")

    apksigner = get_tool(sdk, "apksigner")
    zipalign = get_tool(sdk, "zipalign")
    if not apksigner or not zipalign:
        raise ValueError("Android SDK build-tools missing (apksigner/zipalign required).")

    stripped = work_dir / "stripped.apk"
    await asyncio.to_thread(strip_old_signatures, input_apk, stripped)

    aligned = work_dir / "aligned.apk"
    await on_progress(40, "🔏 Aligning APK (zipalign)...")
    await run_tool([zipalign, "-p", "-f", "4", str(stripped), str(aligned)], on_progress, "zipalign")

    custom_requested = bool(KEYSTORE_JSON and KEYSTORE_JSON.strip())
    try:
        ks_info = get_custom_keystore(work_dir)
    except Exception as e:
        log.warning("get_custom_keystore raised: %s", e)
        CUSTOM_KEY_ERROR = CUSTOM_KEY_ERROR or str(e)[:300]
        ks_info = None
    signed = work_dir / "signed.apk"
    min_sdk = str(MIN_SDK) if str(MIN_SDK).isdigit() else "14"
    max_sdk = str(MAX_SDK) if str(MAX_SDK).isdigit() else ""
    
    ks_success = False
    if ks_info:
        keystore, storepass, keypass, alias, ks_type = ks_info
        await on_progress(55, "🔏 Using your custom signing key...")
        sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", f"pass:{storepass}",
                    "--key-pass", f"pass:{keypass}", "--ks-key-alias", alias,
                    "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
                    "--min-sdk-version", min_sdk]
        if max_sdk:
            sign_cmd.extend(["--max-sdk-version", max_sdk])
        sign_cmd.extend(["--out", str(signed), str(aligned)])
        
        if ks_type:
            sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-type", ks_type,
                        "--ks-pass", f"pass:{storepass}", "--key-pass", f"pass:{keypass}",
                        "--ks-key-alias", alias,
                        "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
                        "--min-sdk-version", min_sdk]
            if max_sdk:
                sign_cmd.extend(["--max-sdk-version", max_sdk])
            sign_cmd.extend(["--out", str(signed), str(aligned)])
        
        try:
            await on_progress(60, f"🔏 Signing APK for Android {min_sdk}+{f' to {max_sdk}' if max_sdk else ''} (v1+v2)...")
            await run_tool(sign_cmd, on_progress, "apksigner")
            ks_success = True
        except Exception as e:
            log.warning("Custom keystore signing failed: %s", e)
            CUSTOM_KEY_ERROR = CUSTOM_KEY_ERROR or f"apksigner rejected the custom key: {e}"
            await on_progress(55, "⚠️ Custom key rejected by apksigner. Falling back to debug key...")
            ks_success = False
    elif custom_requested:
        edit(
            "⚠️ <b>Custom key was requested but could not be used:</b>\n\n"
            f"<code>{CUSTOM_KEY_ERROR or 'Unknown reason'}</code>\n\n"
            "Signing with the <b>debug key</b> instead. Fix the keystore in Settings "
            "(or re-run /setkey) to sign with your own key.",
            parse_mode="HTML", keep_button=False
        )

    if not ks_success:
        if custom_requested and CUSTOM_KEY_ERROR:
            await on_progress(55, "⚠️ Custom key unavailable — using debug key...")
        keystore = await asyncio.to_thread(make_keystore, work_dir / "debug.keystore")
        sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", "pass:android", "--key-pass", "pass:android",
                    "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
                    "--min-sdk-version", min_sdk]
        if max_sdk:
            sign_cmd.extend(["--max-sdk-version", max_sdk])
        sign_cmd.extend(["--out", str(signed), str(aligned)])
        await on_progress(60, f"🔏 Signing APK for Android {min_sdk}+{f' to {max_sdk}' if max_sdk else ''} (v1+v2)...")
        await run_tool(sign_cmd, on_progress, "apksigner")

    await on_progress(80, "🔏 Verifying signature...")
    await run_tool([apksigner, "verify", "-v", str(signed)], on_progress, "apksigner verify")
    await on_progress(90, "✅ APK signed!")
    return signed


async def main():
    if not JOB_ID and (not BOT_TOKEN or not CHAT_ID):
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    edit("🟢 Job started! Preparing APK Sign engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("apksign_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        global TOOL_LOG_FH
        TOOL_LOG_FH = open(work_dir / "build_log.txt", "a", encoding="utf-8", errors="replace")
        ext = Path(FILENAME).suffix or ".apk"
        if ext.lower() != ".apk":
            ext = ".apk"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        dl_method = ["📥 Downloading APK..."]
        last_dl_time = [0.0]
        async def on_dl(pct: float):
            now = time.time()
            if pct < 100.0 and (pct < last[0] or ((pct - last[0] < 5.0) and (now - last_dl_time[0] < 3.0))):
                return
            last[0] = pct
            last_dl_time[0] = now
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

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing to sign...")

        sdk = find_sdk()
        if not sdk["build_tools"]:
            edit("❌ Android SDK build-tools not found on the runner.", keep_button=False)
            return

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "🔏 Signing APK..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            signed = await sign_apk(dest, work_dir, on_progress, sdk)
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The APK is too large to sign.", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "APK Sign failed")
            return

        await on_progress(100, "✅ APK signed!")
        min_sdk = str(MIN_SDK) if str(MIN_SDK).isdigit() else "14"
        caption = f"🔏 <b>Re-signed APK</b> (v1+v2, Android {min_sdk}+) — Powered By @R3V_X"
        if JOB_ID:
            upload_result_for_app(signed)

        elif BOT_TOKEN and BOT_TOKEN != "app_direct_mode" and CHAT_ID:
            try:
                http_ok = False
                if signed.stat().st_size <= 50 * 1024 * 1024:
                    try:
                        with open(signed, "rb") as doc_f:
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
                            sys.executable, "upload_file.py", str(signed), caption,
                            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                        )
                        await proc.wait()
                        if proc.returncode != 0:
                            log.warning("MTProto Upload failed with code %d", proc.returncode)
            except Exception as e:
                log.warning("Telegram upload failed: %s", e)
                if not JOB_ID:
                    await send_error_log(work_dir, e, "Result upload failed")

        edit("✅ APK re-signed and delivered! 🔥", keep_button=False)
    finally:
        if TOOL_LOG_FH is not None:
            try:
                TOOL_LOG_FH.close()
            except Exception:
                pass
        shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except JobCancelled:
        pass
