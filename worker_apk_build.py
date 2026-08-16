import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from html import unescape
from pathlib import Path
from urllib.parse import unquote

import httpx

from worker_cc_compile import CC_EXTENSIONS, CPP_EXTENSIONS, find_ndk_bin

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("worker_apk_build")

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
R8_JAR = os.environ.get("PAYLOAD_R8_JAR", "/opt/r8.jar")
KOTLINC_ROOT = os.environ.get("PAYLOAD_KOTLINC", "/opt/kotlinc")
APKTOOL_JAR = os.environ.get("PAYLOAD_APKTOOL_JAR", "/opt/apktool/apktool.jar")
KEYSTORE_JSON = os.environ.get("PAYLOAD_CUSTOM_KEYSTORE_JSON") or os.environ.get("PAYLOAD_KEYSTORE", "")
CUSTOM_KEY_ERROR = ""  # human-readable reason when the custom keystore cannot be used
PAYLOAD_BUILD_MODE = os.environ.get("PAYLOAD_BUILD_MODE", "auto").lower()
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 1000

JAVA_EXTENSIONS = {".java"}
KOTLIN_EXTENSIONS = {".kt"}

ABI_CLANG_NAMES = {
    "arm64-v8a": ["aarch64-linux-android24-clang", "aarch64-linux-android-clang"],
    "armeabi-v7a": ["armv7a-linux-androideabi24-clang", "armv7a-linux-androideabi-clang"],
    "x86_64": ["x86_64-linux-android24-clang", "x86_64-linux-android-clang"],
}

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


def upload_to_anonymous_cloud(file_path: Path) -> str:
    """Upload result to anonymous public host for 1-click mobile download."""
    if not file_path or not file_path.exists():
        return ""
    log.info("Uploading %s to anonymous cloud hosts...", file_path.name)
    
    # 1. tmpfiles.org
    try:
        with open(file_path, "rb") as fh:
            resp = httpx.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": (file_path.name, fh)},
                timeout=120
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                url = data.get("url", "")
                if url:
                    direct_url = url.replace("https://tmpfiles.org/", "https://tmpfiles.org/dl/")
                    log.info("tmpfiles upload success: %s", direct_url)
                    return direct_url
    except Exception as e:
        log.warning("tmpfiles upload error: %s", e)

    # 2. catbox.moe
    try:
        with open(file_path, "rb") as fh:
            resp = httpx.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (file_path.name, fh)},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=120
            )
            if resp.status_code == 200 and resp.text.strip().startswith("http"):
                log.info("catbox upload success: %s", resp.text.strip())
                return resp.text.strip()
    except Exception as e:
        log.warning("catbox upload error: %s", e)

    # 3. uguu.se
    try:
        with open(file_path, "rb") as fh:
            resp = httpx.post(
                "https://uguu.se/upload",
                files={"files[]": (file_path.name, fh)},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=120
            )
            if resp.status_code == 200:
                files_arr = resp.json().get("files", [])
                if files_arr and "url" in files_arr[0]:
                    log.info("uguu upload success: %s", files_arr[0]["url"])
                    return files_arr[0]["url"]
    except Exception as e:
        log.warning("uguu upload error: %s", e)

    # 4. file.io
    try:
        with open(file_path, "rb") as fh:
            resp = httpx.post(
                "https://file.io/",
                files={"file": (file_path.name, fh)},
                timeout=120
            )
            if resp.status_code == 200:
                link = resp.json().get("link", "")
                if link:
                    log.info("file.io upload success: %s", link)
                    return link
    except Exception as e:
        log.warning("file.io upload error: %s", e)

    return ""


def upload_result_for_app(file_to_send: Path):
    if not JOB_ID or not file_to_send or not file_to_send.exists():
        return

    target_chat = CHAT_ID if CHAT_ID and CHAT_ID != "0" and not str(CHAT_ID).startswith("app_") else "me"

    api_id_val = os.environ.get("API_ID", "").strip()
    api_hash_val = os.environ.get("API_HASH", "").strip()
    bot_tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

    # 1) Preferred: deliver straight to the user's chat via the Telegram Bot HTTP API
    if bot_tok and target_chat != "me" and file_to_send.stat().st_size <= 50 * 1024 * 1024:
        try:
            with open(file_to_send, "rb") as fh:
                resp = httpx.post(
                    f"https://api.telegram.org/bot{bot_tok}/sendDocument",
                    data={"chat_id": target_chat, "caption": f"✅ Result for Job {JOB_ID}"},
                    files={"document": (file_to_send.name, fh, "application/octet-stream")},
                    timeout=300,
                )
                if resp.status_code == 200 and resp.json().get("ok"):
                    log.info("Result uploaded to chat %s via Bot HTTP API", target_chat)
                    bot_id = ""
                    try:
                        me = httpx.get(f"https://api.telegram.org/bot{bot_tok}/getMe", timeout=30).json()
                        if me.get("ok"):
                            bot_id = str(me["result"].get("id", ""))
                    except Exception:
                        pass
                    if bot_id:
                        notify_app(f"FINAL_ZIP_URL:telegram_msg:{bot_id}")
                    else:
                        notify_app("FINAL_ZIP_URL:telegram_direct_upload")
                    return
                log.warning("Bot HTTP upload failed (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as e:
            log.warning("Bot HTTP upload failed: %s", e)

    # 2) MTProto upload (requires API_ID/API_HASH secrets on runner)
    if api_id_val and api_hash_val:
        try:
            cmd = [sys.executable, "upload_file.py", str(file_to_send), f"✅ Result for Job {JOB_ID}", str(target_chat)]
            proc = subprocess.run(cmd, timeout=300, capture_output=True, text=True)
            if proc.returncode == 0:
                log.info("Uploaded to Telegram MTProto successfully: %s", proc.stdout)
                for line in proc.stdout.splitlines():
                    if line.startswith("UPLOAD_SUCCESS:"):
                        parts = line.split(":")
                        if len(parts) >= 2:
                            bot_username = parts[1]
                            notify_app(f"FINAL_ZIP_URL:telegram_deeplink:{bot_username}")
                            return
                notify_app("FINAL_ZIP_URL:telegram_direct_upload")
                return
            else:
                log.warning("MTProto upload failed with code %d: %s", proc.returncode, proc.stderr)
        except Exception as e:
            log.warning("MTProto upload failed: %s", e)

    # 3) Direct Cloud Upload Fallback for standalone mobile app download
    cloud_url = upload_to_anonymous_cloud(file_to_send)
    if cloud_url:
        notify_app(f"FINAL_ZIP_URL:{cloud_url}")
        return

    notify_app("FINAL_ZIP_URL:telegram_direct_upload")


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


async def send_error_log(work_dir, exception_obj, title="APK Build failed"):
    import traceback
    err_str = traceback.format_exc()
    log.error("%s: %s", title, exception_obj)
    sent = False
    try:
        err_file = Path(work_dir) / "error.txt"
        text = f"❌ {title}:\n\n{err_str}"
        err_file.write_text(text, encoding="utf-8")
        if BOT_TOKEN and CHAT_ID and BOT_TOKEN != "app_direct_mode":
            await upload_document(err_file, f"❌ <b>{title}</b>")
            sent = True
    except Exception as e:
        log.error("Failed to upload error log: %s", e)

    notify_app(f"❌ {title}: {str(exception_obj)[:150]}")
    if not sent:
        edit(f"❌ <b>{title}</b>\n\n<code>{str(exception_obj)[:300]}</code>", parse_mode="HTML", keep_button=False)


async def download_url(url: str, dest: Path, on_progress) -> str:
    last_prog = [-1]
    filename = FILENAME or "download.zip"
    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=600)) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)', cd, re.I)
            if m:
                filename = unquote(m.group(1).strip())
            total = int(resp.headers.get("content-length") or 0)
            check_download_size(total)
            done = 0
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(65536):
                    if CANCELLED["v"]:
                        raise JobCancelled()
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = int(done * 100 / total)
                        if pct != last_prog[0]:
                            last_prog[0] = pct
                            await on_progress(pct)
    return filename


async def run_tool(cmd: list, on_progress, label: str, timeout: int = 86400, progress_stall: int = 1800, cwd: str = None):
    log.info("Running [%s] in %s: %s", label, cwd or ".", " ".join(str(x) for x in cmd))
    cmd_str = [str(x) for x in cmd]
    proc = await asyncio.create_subprocess_exec(
        *cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd
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
        raise RuntimeError(f"{label} failed with exit code {rc}:\n" + "\n".join(out_lines[-30:]))
    return "\n".join(out_lines[-30:])


def find_inputs(src_dir: Path, suffixes) -> list:
    return [str(p) for p in sorted(src_dir.rglob("*")) if p.is_file() and p.suffix.lower() in suffixes]


def kotlinc_bin():
    k = os.path.join(KOTLINC_ROOT, "bin", "kotlinc")
    if os.path.isfile(k):
        return k
    return shutil.which("kotlinc")


def find_sdk():
    sdk_root = SDK_ROOT or os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk"
    if not os.path.isdir(sdk_root):
        cand = [p for p in ("/usr/local/lib/android/sdk", "/opt/android-sdk", os.path.expanduser("~/Android/Sdk")) if os.path.isdir(p)]
        if cand:
            sdk_root = cand[0]

    build_tools = ""
    bt_dir = os.path.join(sdk_root, "build-tools")
    if os.path.isdir(bt_dir):
        versions = sorted(os.listdir(bt_dir), reverse=True)
        if versions:
            build_tools = os.path.join(bt_dir, versions[0])

    platforms = []
    p_dir = os.path.join(sdk_root, "platforms")
    if os.path.isdir(p_dir):
        platforms = sorted([os.path.join(p_dir, p) for p in os.listdir(p_dir) if p.startswith("android-")])

    return {"root": sdk_root, "build_tools": build_tools, "platforms": platforms}


def get_tool(sdk, name):
    if sdk["build_tools"]:
        t = os.path.join(sdk["build_tools"], name)
        if os.path.isfile(t):
            return t
    return shutil.which(name)


def get_android_jar(sdk):
    if sdk["platforms"]:
        for p in reversed(sdk["platforms"]):
            j = os.path.join(p, "android.jar")
            if os.path.isfile(j):
                m = re.search(r"android-(\d+)", p)
                api = int(m.group(1)) if m else 34
                return j, api
    return "", 0


def parse_manifest(path: Path):
    if not path.exists():
        return {"package": "", "min_sdk": 21, "target_sdk": 34, "version_code": "1", "version_name": "1.0"}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        pkg = re.search(r'package\s*=\s*["\']([^"\']+)["\']', text)
        min_sdk = re.search(r'android:minSdkVersion\s*=\s*["\'](\d+)["\']', text)
        target_sdk = re.search(r'android:targetSdkVersion\s*=\s*["\'](\d+)["\']', text)
        vcode = re.search(r'android:versionCode\s*=\s*["\'](\d+)["\']', text)
        vname = re.search(r'android:versionName\s*=\s*["\']([^"\']+)["\']', text)
        return {
            "package": pkg.group(1) if pkg else "",
            "min_sdk": int(min_sdk.group(1)) if min_sdk else 21,
            "target_sdk": int(target_sdk.group(1)) if target_sdk else 34,
            "version_code": vcode.group(1) if vcode else "1",
            "version_name": vname.group(1) if vname else "1.0",
        }
    except Exception:
        return {"package": "", "min_sdk": 21, "target_sdk": 34, "version_code": "1", "version_name": "1.0"}


def ensure_manifest_package(manifest: Path, extract_dir: Path) -> str:
    text = manifest.read_text(encoding="utf-8", errors="replace") if manifest.exists() else ""
    pkg = ""
    # 1. Search build.gradle/settings.gradle
    for bg in extract_dir.rglob("build.gradle*"):
        try:
            bgt = bg.read_text(errors="replace")
            m = re.search(r'namespace\s*[=\s]\s*["\']([^"\']+)["\']', bgt) or re.search(r'applicationId\s*[=\s]\s*["\']([^"\']+)["\']', bgt)
            if m:
                pkg = m.group(1).strip()
                break
        except Exception:
            pass

    # 2. Search Java/Kotlin sources
    if not pkg:
        for p in sorted(extract_dir.rglob("*.java")) + sorted(extract_dir.rglob("*.kt")):
            try:
                t = p.read_text(errors="replace")
                pm = re.search(r"package\s+([a-zA-Z0-9_.]+)", t)
                if pm:
                    pkg = pm.group(1).strip()
                    break
            except Exception:
                pass
    if not pkg:
        pkg = "com.example.app"

    if manifest.exists() and "<manifest" in text and "package=" not in text:
        text = text.replace("<manifest", f'<manifest package="{pkg}"', 1)
        manifest.write_text(text, encoding="utf-8")
    return pkg


def sanitize_and_decode_xml_files(extract_dir: Path):
    """Sanitize XML files: decode UTF-16 to UTF-8, strip UTF-8 BOM, strip leading/trailing non-XML garbage."""
    for p in sorted(extract_dir.rglob("*")):
        if not p.is_file() or (p.suffix.lower() != ".xml" and p.name.lower() != "androidmanifest.xml"):
            continue
        try:
            raw = p.read_bytes()
            if not raw:
                continue
            # UTF-16 LE / BE detection
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                try:
                    text = raw.decode("utf-16")
                    raw = text.encode("utf-8")
                    log.info("Converted UTF-16 XML to UTF-8: %s", p.name)
                except Exception:
                    pass
            # UTF-8 BOM detection
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]

            # Strip leading non-XML bytes up to '<' if it's text XML
            if not raw.startswith(b"\x03\x00\x08\x00"):
                idx = raw.find(b"<")
                if idx > 0:
                    raw = raw[idx:]
                last_idx = raw.rfind(b">")
                if last_idx != -1 and last_idx < len(raw) - 1:
                    raw = raw[:last_idx + 1]

            p.write_bytes(raw)
        except Exception as e:
            log.warning("XML sanitization error on %s: %s", p, e)


def sanitize_and_validate_manifest(manifest: Path, extract_dir: Path):
    """Ensure AndroidManifest.xml is 100% well-formed Text XML (not binary AXML)."""
    if not manifest.exists():
        return
    try:
        raw = manifest.read_bytes()
        if not raw:
            return

        # Check if Binary AXML (magic header 0x00080003)
        is_binary_axml = raw.startswith(b"\x03\x00\x08\x00") or (len(raw) > 8 and raw[0] == 0x03 and raw[1] == 0x00)
        if is_binary_axml:
            log.info("Detected Binary AndroidManifest.xml (AXML). Converting to valid Text XML...")
            strings = []
            try:
                pos = 8
                while pos < len(raw) - 4:
                    chunk = raw[pos:pos+256]
                    found = re.findall(rb'[a-zA-Z0-9_.\-\:\/]{3,}', chunk)
                    for f in found:
                        try:
                            s = f.decode('ascii')
                            if len(s) > 2 and s not in strings:
                                strings.append(s)
                        except Exception:
                            pass
                    pos += 128
            except Exception:
                pass

            pkg = "com.example.app"
            for s in strings:
                if "." in s and not s.startswith("http") and not s.startswith("android") and not s.endswith(".xml") and not s.endswith(".png"):
                    if re.match(r'^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$', s):
                        pkg = s
                        break

            clean_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{pkg}">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="34" />
    <application
        android:label="App"
        android:allowBackup="true"
        android:supportsRtl="true">
        <activity android:name=".MainActivity" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>
    </application>
</manifest>
"""
            manifest.write_text(clean_xml.strip(), encoding="utf-8")
            log.info("Replaced binary AndroidManifest.xml with clean Text XML for %s", pkg)
            return

        # It is text XML: clean up encodings and validate
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = raw.decode("utf-16")
            except UnicodeDecodeError:
                text = raw.decode("latin1", errors="replace")

        # Strip any leading garbage before '<'
        idx = text.find("<")
        if idx > 0:
            text = text[idx:]

        # Strip trailing nulls/garbage
        last_idx = text.rfind(">")
        if last_idx != -1 and last_idx < len(text) - 1:
            text = text[:last_idx + 1]

        # Fix unescaped ampersands
        fixed = re.sub(r'&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)', '&amp;', text)
        
        # Test parse with ElementTree
        try:
            import xml.etree.ElementTree as ET
            ET.fromstring(fixed.encode("utf-8"))
            manifest.write_text(fixed, encoding="utf-8")
            log.info("Validated and sanitized AndroidManifest.xml successfully")
        except Exception as pe:
            log.warning("Manifest XML parse validation warning: %s, repairing root structure...", pe)
            pkg = ensure_manifest_package(manifest, extract_dir)
            repair_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{pkg}">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="34" />
    <application
        android:label="App"
        android:allowBackup="true"
        android:supportsRtl="true">
    </application>
</manifest>
"""
            manifest.write_text(repair_xml.strip(), encoding="utf-8")
            log.info("Repaired AndroidManifest.xml with package: %s", pkg)
    except Exception as e:
        log.warning("sanitize_and_validate_manifest error: %s", e)


def sanitize_res_directories(extract_dir: Path):
    """Fix common resource directory naming issues for AAPT2.
    Specifically, <adaptive-icon> in *-anydpi folders without -v26 qualifier causes AAPT2 link error."""
    for res_dir in sorted(extract_dir.rglob("res")):
        if not res_dir.is_dir():
            continue
        for sub in list(res_dir.iterdir()):
            if not sub.is_dir():
                continue
            name = sub.name.lower()
            # If folder is -anydpi without -v26+ qualifier
            if "anydpi" in name and not any(f"-v{v}" in name for v in range(26, 36)):
                new_name = f"{name}-v26"
                target = res_dir / new_name
                try:
                    if target.exists():
                        for f in sub.iterdir():
                            shutil.move(str(f), str(target / f.name))
                        try:
                            sub.rmdir()
                        except Exception:
                            pass
                    else:
                        sub.rename(target)
                    log.info("Renamed %s -> %s for adaptive-icon support", name, new_name)
                except Exception as e:
                    log.warning("Could not rename %s: %s", sub, e)


def extract_archive(input_path: Path, extract_dir: Path):
    """Robustly extract any archive type or copy raw source file."""
    extract_dir.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(input_path):
        with zipfile.ZipFile(input_path, "r") as zf:
            zf.extractall(extract_dir)
    elif tarfile.is_tarfile(input_path):
        with tarfile.open(input_path, "r:*") as tf:
            tf.extractall(extract_dir)
    else:
        # Check if 7z or single source file
        try:
            proc = subprocess.run(["7z", "x", "-y", f"-o{extract_dir}", str(input_path)], capture_output=True)
            if proc.returncode == 0:
                return
        except Exception:
            pass
        # Raw file fallback (single .java / .kt / .smali)
        dest = extract_dir / input_path.name
        shutil.copy2(input_path, dest)


def ensure_apktool_yml(target_dir: Path):
    """Ensure a valid apktool.yml exists in target_dir for decompiled / smali projects."""
    yml_file = target_dir / "apktool.yml"
    if not yml_file.exists():
        manifest_path = target_dir / "AndroidManifest.xml"
        if not manifest_path.exists():
            cand = [p for p in target_dir.rglob("*") if p.is_file() and p.name.lower() == "androidmanifest.xml"]
            if cand:
                manifest_path = cand[0]
        mp = parse_manifest(manifest_path) if manifest_path.exists() else {}
        min_sdk = mp.get("min_sdk", 21) or 21
        target_sdk = mp.get("target_sdk", 34) or 34
        vcode = mp.get("version_code", "1") or "1"
        vname = mp.get("version_name", "1.0") or "1.0"
        
        yml_content = f"""version: 2.10.0
apkFileName: app.apk
isFrameworkApk: false
usesFramework:
  ids:
  - 1
packageInfo:
  forcedPackageId: '127'
sdkInfo:
  minSdkVersion: '{min_sdk}'
  targetSdkVersion: '{target_sdk}'
versionInfo:
  versionCode: '{vcode}'
  versionName: '{vname}'
doNotCompress:
- resources.arsc
- png
- so
"""
        yml_file.write_text(yml_content, encoding="utf-8")
        log.info("Auto-generated apktool.yml in %s", target_dir)


async def build_apk_from_source(input_path: Path, work_dir: Path, on_progress, sdk):
    await on_progress(10, "📦 Extracting project source code...")
    extract_dir = work_dir / "src"
    extract_archive(input_path, extract_dir)

    # If single top-level folder inside zip, normalize root
    entries = [p for p in extract_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        extract_dir = entries[0]

    # Sanitize XML files (UTF-16 -> UTF-8, strip BOM)
    sanitize_and_decode_xml_files(extract_dir)
    # Sanitize Resource directories (adaptive-icon v26 qualifiers)
    sanitize_res_directories(extract_dir)

    # Check project features
    gradlew = None
    build_gradle = None
    settings_gradle = None

    for g in sorted(extract_dir.rglob("gradlew")):
        if g.is_file():
            gradlew = g
            break
    for s in sorted(extract_dir.rglob("settings.gradle")) + sorted(extract_dir.rglob("settings.gradle.kts")):
        if s.is_file():
            settings_gradle = s
            break
    for b in sorted(extract_dir.rglob("build.gradle")) + sorted(extract_dir.rglob("build.gradle.kts")):
        if b.is_file():
            build_gradle = b
            break

    has_gradle = bool(gradlew) or bool(build_gradle) or bool(settings_gradle)

    target_apktool_dir = None
    for root, dirs, files in os.walk(extract_dir):
        if "apktool.yml" in files:
            target_apktool_dir = Path(root)
            break
        if "AndroidManifest.xml" in files and ("res" in dirs or any(d.startswith("smali") for d in dirs) or "lib" in dirs or "assets" in dirs):
            target_apktool_dir = Path(root)
            break
        if any(d.startswith("smali") for d in dirs):
            target_apktool_dir = Path(root)
            break
        if "AndroidManifest.xml" in files:
            target_apktool_dir = Path(root)
            break

    smali_dirs = [p for p in sorted(extract_dir.rglob("smali*")) if p.is_dir()]
    has_smali = bool(smali_dirs) or bool(target_apktool_dir)
    java_kt_files = [p for p in sorted(extract_dir.rglob("*")) if p.is_file() and p.suffix.lower() in (JAVA_EXTENSIONS | KOTLIN_EXTENSIONS)]
    has_java_kt = bool(java_kt_files)
    has_apktool_project = bool(target_apktool_dir) or bool(smali_dirs) or (extract_dir / "AndroidManifest.xml").exists()

    # Determine Build Mode
    build_mode = PAYLOAD_BUILD_MODE
    if build_mode in ("auto", ""):
        if has_gradle:
            build_mode = "gradle"
        elif has_apktool_project:
            # Match Telegram Bot behavior 100%: Rebuild using full Apktool Engine (produces full 80+ MB APK)
            build_mode = "apktool"
        elif has_java_kt:
            build_mode = "manifest"
        else:
            build_mode = "apktool" if has_apktool_project else "manifest"

    log.info("Selected APK build mode: %s (has_gradle=%s, has_apktool=%s, has_smali=%s, has_java_kt=%s)",
             build_mode, has_gradle, bool(target_apktool_dir), has_smali, has_java_kt)

    # =========================================================================
    # 1. GRADLE BUILD MODE
    # =========================================================================
    if build_mode == "gradle":
        await on_progress(20, "🚀 Building Android project with Gradle...")
        if settings_gradle:
            target_dir = settings_gradle.parent
        elif gradlew:
            target_dir = gradlew.parent
        elif build_gradle:
            target_dir = build_gradle.parent
        else:
            target_dir = extract_dir

        sdk_path = sdk["root"]
        ndk_bin = find_ndk_bin()
        ndk_path = str(Path(ndk_bin).parents[3]) if ndk_bin else ""

        # Write local.properties so AGP never complains about missing SDK location
        local_prop = target_dir / "local.properties"
        lp_content = f"sdk.dir={sdk_path}\n"
        if ndk_path:
            lp_content += f"ndk.dir={ndk_path}\n"
        local_prop.write_text(lp_content, encoding="utf-8")
        log.info("Wrote local.properties with sdk.dir=%s", sdk_path)

        # Write gradle.properties with memory optimization & AndroidX
        gp = target_dir / "gradle.properties"
        gp_lines = gp.read_text(errors="replace").splitlines() if gp.exists() else []
        gp_map = {}
        for line in gp_lines:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                gp_map[k.strip()] = v.strip()
        gp_map.setdefault("org.gradle.jvmargs", "-Xmx4096m -XX:MaxMetaspaceSize=1024m -Dfile.encoding=UTF-8")
        gp_map.setdefault("android.useAndroidX", "true")
        gp_map.setdefault("android.enableJetifier", "true")
        gp_map.setdefault("android.nonTransitiveRClass", "false")
        gp.write_text("\n".join(f"{k}={v}" for k, v in gp_map.items()) + "\n", encoding="utf-8")

        # Setup wrapper or gradlew
        if not gradlew:
            gw_jar = target_dir / "gradle" / "wrapper" / "gradle-wrapper.jar"
            if not gw_jar.exists():
                try:
                    await on_progress(22, "⚙️ Generating Gradle wrapper (8.10.2)...")
                    await run_tool(["gradle", "wrapper", "--gradle-version", "8.10.2"], on_progress, "gradle wrapper", cwd=str(target_dir))
                except Exception as we:
                    log.warning("System gradle wrapper failed: %s", we)
            cand = target_dir / "gradlew"
            if cand.is_file():
                gradlew = cand

        gradle_cmd = []
        if gradlew and gradlew.exists():
            os.chmod(gradlew, 0o755)
            gradle_cmd = ["./gradlew", "assembleDebug", "--no-daemon", "--stacktrace"]
        else:
            gradle_cmd = ["gradle", "assembleDebug", "--no-daemon", "--stacktrace"]

        try:
            await on_progress(28, "⚡ Compiling APK with Gradle...")
            await run_tool(gradle_cmd, on_progress, "gradle assembleDebug", cwd=str(target_dir))
        except Exception as ge:
            log.warning("Gradle build error: %s", ge)
            if has_apktool_project:
                await on_progress(35, "⚠️ Gradle assemble failed. Rebuilding with Apktool...")
                build_mode = "apktool"
            elif has_java_kt:
                await on_progress(35, "⚠️ Gradle assemble failed. Falling back to Native Engine...")
                build_mode = "manifest"
            else:
                raise ValueError(f"Gradle build failed: {ge}")

        if build_mode == "gradle":
            apks = sorted(target_dir.rglob("*.apk"))
            # Filter out unaligned/intermediate apks if debug apk exists
            debug_apks = [p for p in apks if "-debug" in p.name.lower() or "app-debug" in p.name.lower()]
            found_apk = debug_apks[-1] if debug_apks else (apks[-1] if apks else None)
            if not found_apk:
                if has_apktool_project:
                    log.info("No APK from Gradle, trying Apktool...")
                    build_mode = "apktool"
                elif has_java_kt:
                    log.info("No APK from Gradle, trying Native Engine...")
                    build_mode = "manifest"
                else:
                    raise ValueError("Gradle build completed but no .apk output was produced.")
            else:
                return await finalize_apk_signing(found_apk, work_dir, on_progress, sdk)

    # =========================================================================
    # 2. APKTOOL / SMALI BUILD MODE (MATCHES TELEGRAM BOT BUILD 100%)
    # =========================================================================
    if build_mode == "apktool":
        await on_progress(25, "🔨 Rebuilding APK with Apktool Engine (Full Project Build)...")
        apktool_dir = target_apktool_dir if target_apktool_dir else extract_dir
        ensure_apktool_yml(apktool_dir)
        unsigned_apk = work_dir / "apktool_unsigned.apk"
        
        apktool_jar_path = Path(APKTOOL_JAR)
        if not apktool_jar_path.exists():
            # Download Apktool jar if missing
            apktool_jar_path.parent.mkdir(parents=True, exist_ok=True)
            dl_cmd = ["curl", "-sL", "-o", str(apktool_jar_path), "https://github.com/iBotPeaches/Apktool/releases/download/v2.10.0/apktool_2.10.0.jar"]
            subprocess.run(dl_cmd, timeout=120)

        if apktool_jar_path.exists():
            cmd = ["java", "-Xmx8G", "-jar", str(apktool_jar_path), "b", str(apktool_dir), "-o", str(unsigned_apk)]
            try:
                await run_tool(cmd, on_progress, "apktool build")
                if unsigned_apk.exists() and unsigned_apk.stat().st_size > 10240:
                    return await finalize_apk_signing(unsigned_apk, work_dir, on_progress, sdk)
            except Exception as ae:
                log.warning("Apktool build failed: %s", ae)
                if has_java_kt:
                    await on_progress(35, "⚠️ Apktool failed. Falling back to Native Engine...")
                    build_mode = "manifest"
                else:
                    raise ValueError(f"Apktool build failed: {ae}")

    # =========================================================================
    # 3. NATIVE ENGINE (AAPT2 + JAVAC/KOTLINC + D8 + NDK)
    # =========================================================================
    await on_progress(20, "🚀 Building APK using Native Cloud Engine...")

    # Locate or generate AndroidManifest.xml
    manifest = extract_dir / "AndroidManifest.xml"
    if not manifest.exists():
        cand_m = [p for p in extract_dir.rglob("*") if p.is_file() and p.name.lower() == "androidmanifest.xml"]
        if cand_m:
            manifest = cand_m[0]
        else:
            # Auto-generate standard manifest
            pkg = "com.example.app"
            main_activity = ""
            for p in sorted(extract_dir.rglob("*.java")) + sorted(extract_dir.rglob("*.kt")):
                try:
                    t = p.read_text(errors="replace")
                    pm = re.search(r"package\s+([a-zA-Z0-9_.]+)", t)
                    if pm:
                        pkg = pm.group(1).strip()
                    if "Activity" in p.name or "onCreate" in t:
                        cls_m = re.search(r"class\s+([a-zA-Z0-9_]+)", t)
                        if cls_m:
                            main_activity = f".{cls_m.group(1)}"
                    if pkg and main_activity:
                        break
                except Exception:
                    pass

            manifest = extract_dir / "AndroidManifest.xml"
            act_xml = ""
            if main_activity:
                act_xml = f"""
        <activity android:name="{main_activity}" android:exported="true">
            <intent-filter>
                <action android:name="android.intent.action.MAIN" />
                <category android:name="android.intent.category.LAUNCHER" />
            </intent-filter>
        </activity>"""

            manifest_content = f"""<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android"
    package="{pkg}">
    <uses-sdk android:minSdkVersion="21" android:targetSdkVersion="34" />
    <application
        android:label="App"
        android:allowBackup="true"
        android:supportsRtl="true">
        {act_xml}
    </application>
</manifest>
"""
            manifest.write_text(manifest_content.strip(), encoding="utf-8")

    sanitize_and_validate_manifest(manifest, extract_dir)
    mp = parse_manifest(manifest)
    if not mp["package"]:
        mp["package"] = ensure_manifest_package(manifest, extract_dir)

    VALID_RES_PREFIXES = (
        "anim", "animator", "color", "drawable", "font", "interpolator",
        "layout", "menu", "mipmap", "navigation", "raw", "transition", "values", "xml"
    )

    def is_valid_android_res_dir(r_path: Path) -> bool:
        if not r_path.is_dir():
            return False
        if any(part.lower() == "assets" for part in r_path.parts):
            return False
        if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git", "test", "androidTest") for part in r_path.parts):
            return False
        subdirs = [p.name.lower() for p in r_path.iterdir() if p.is_dir()]
        if not subdirs:
            return any(p.suffix.lower() in (".xml", ".png", ".jpg", ".webp", ".9.png") for p in r_path.iterdir() if p.is_file())
        for s in subdirs:
            base_type = s.split("-")[0]
            if base_type in VALID_RES_PREFIXES:
                return True
        return False

    res_dirs = [r for r in sorted(extract_dir.rglob("res")) if is_valid_android_res_dir(r)]
    if not res_dirs:
        dummy_res = work_dir / "default_res"
        (dummy_res / "values").mkdir(parents=True, exist_ok=True)
        (dummy_res / "values" / "strings.xml").write_text('<resources><string name="app_name">App</string></resources>', encoding="utf-8")
        res_dirs.append(dummy_res)

    assets_dirs = []
    for a in sorted(extract_dir.rglob("assets")):
        if a.is_dir() and any(a.iterdir()):
            if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git", "test") for part in a.parts):
                continue
            if not any(a != ex and str(a).startswith(str(ex)) for ex in assets_dirs):
                assets_dirs.append(a)

    jni_dirs = []
    for j in sorted(extract_dir.rglob("jniLibs")) + sorted(extract_dir.rglob("libs")):
        if j.is_dir() and any(j.rglob("*.so")):
            if not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git", "test") for part in j.parts):
                jni_dirs.append(j)

    libs_dirs = []
    for l in sorted(extract_dir.rglob("libs")):
        if l.is_dir() and any(l.rglob("*.jar")):
            if not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git", "test") for part in l.parts):
                libs_dirs.append(l)

    java_files = []
    kt_files = []
    for p in sorted(extract_dir.rglob("*")):
        if p.is_file():
            if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git", "test", "androidTest") for part in p.parts):
                continue
            if p.suffix.lower() in JAVA_EXTENSIONS:
                java_files.append(str(p))
            elif p.suffix.lower() in KOTLIN_EXTENSIONS:
                kt_files.append(str(p))
    java_files = sorted(set(java_files))
    kt_files = sorted(set(kt_files))

    aapt2 = get_tool(sdk, "aapt2")
    android_jar, compile_api = get_android_jar(sdk)
    if not aapt2:
        raise ValueError("Android SDK build-tools missing (aapt2 required).")
    if not android_jar:
        raise ValueError("No Android platform (android.jar) found in SDK.")

    target_sdk = mp["target_sdk"] or (compile_api or 34)
    min_sdk = mp["min_sdk"] or 21
    package = mp["package"] or "com.example.app"

    # Compile Resources
    await on_progress(25, "📦 Compiling resources (aapt2)...")
    compiled_res_list = []
    for idx, r_dir in enumerate(res_dirs):
        c_res = work_dir / f"compiled_res_{idx}.zip"
        try:
            staged = work_dir / f"res_stage_{idx}"
            if staged.exists():
                shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(r_dir, staged, ignore=shutil.ignore_patterns("public.xml"))
            sanitize_res_directories(staged)
            await run_tool([aapt2, "compile", "--dir", str(staged), "-o", str(c_res)], on_progress, f"aapt2 compile res_{idx}")
            if c_res.exists():
                compiled_res_list.append(str(c_res))
        except Exception as res_err:
            log.warning("Skipping non-standard resource directory %s: %s", r_dir, res_err)

    if not compiled_res_list:
        fallback_res = work_dir / "fallback_res"
        (fallback_res / "values").mkdir(parents=True, exist_ok=True)
        (fallback_res / "values" / "strings.xml").write_text('<resources><string name="app_name">App</string></resources>', encoding="utf-8")
        c_res = work_dir / "compiled_res_fallback.zip"
        await run_tool([aapt2, "compile", "--dir", str(fallback_res), "-o", str(c_res)], on_progress, "aapt2 compile fallback_res")
        if c_res.exists():
            compiled_res_list.append(str(c_res))

    # Link Resources
    gen_dir = work_dir / "gen"
    gen_dir.mkdir(exist_ok=True)
    base_apk = work_dir / "base.apk"
    link_cmd = [
        aapt2, "link", "-o", str(base_apk), "-I", android_jar,
        "--manifest", str(manifest),
        "--java", str(gen_dir),
        "--min-sdk-version", str(min_sdk),
        "--target-sdk-version", str(target_sdk),
        "--auto-add-overlay",
        "--no-version-vectors"
    ]
    if mp["version_code"]:
        link_cmd += ["--version-code", str(mp["version_code"])]
    if mp["version_name"]:
        link_cmd += ["--version-name", str(mp["version_name"])]
    for a_dir in assets_dirs:
        link_cmd += ["-A", str(a_dir)]
    for c_res in compiled_res_list:
        link_cmd += [c_res]

    try:
        await run_tool(link_cmd, on_progress, "aapt2 link")
    except Exception as le:
        log.warning("aapt2 link attempt 1 failed: %s, retrying with min-sdk 26...", le)
        link_cmd_retry = [
            aapt2, "link", "-o", str(base_apk), "-I", android_jar,
            "--manifest", str(manifest),
            "--java", str(gen_dir),
            "--min-sdk-version", "26",
            "--target-sdk-version", str(target_sdk or 34),
            "--auto-add-overlay",
            "--no-version-vectors"
        ]
        if mp["version_code"]:
            link_cmd_retry += ["--version-code", str(mp["version_code"])]
        if mp["version_name"]:
            link_cmd_retry += ["--version-name", str(mp["version_name"])]
        for a_dir in assets_dirs:
            link_cmd_retry += ["-A", str(a_dir)]
        for c_res in compiled_res_list:
            link_cmd_retry += [c_res]
        await run_tool(link_cmd_retry, on_progress, "aapt2 link retry")

    if not base_apk.exists():
        raise ValueError("aapt2 link produced no APK (check AndroidManifest.xml).")

    # Compiling Code (Kotlin + Java + Smali)
    class_dirs = []
    cp_parts = [android_jar, str(gen_dir)]
    lib_jars = []
    for l_dir in libs_dirs:
        lib_jars += find_inputs(l_dir, {".jar"})
    cp_parts += lib_jars

    if kt_files:
        kbin = kotlinc_bin()
        if kbin:
            kotlin_out = work_dir / "kotlin_out"
            kotlin_out.mkdir(exist_ok=True)
            await on_progress(40, "☕ Compiling Kotlin sources...")
            kcmd = ["bash", kbin, "-classpath", ":".join(cp_parts), "-jvm-target", "1.8", "-d", str(kotlin_out)] + kt_files
            await run_tool(kcmd, on_progress, "kotlinc")
            class_dirs.append(kotlin_out)
            cp_parts.append(str(kotlin_out))
            stdlib = os.path.join(KOTLINC_ROOT, "lib", "kotlin-stdlib.jar")
            if os.path.isfile(stdlib):
                cp_parts.append(stdlib)
                lib_jars.append(stdlib)

    r_java = find_inputs(gen_dir, JAVA_EXTENSIONS)
    if java_files or r_java:
        java_out = work_dir / "java_out"
        java_out.mkdir(exist_ok=True)
        await on_progress(50, "☕ Compiling Java sources...")
        jcmd = ["javac", "-encoding", "UTF-8", "-classpath", ":".join(cp_parts), "-d", str(java_out)] + r_java + java_files
        await run_tool(jcmd, on_progress, "javac")
        class_dirs.append(java_out)

    # DEX Generation
    dex_files = []
    class_files = []
    for d in class_dirs:
        class_files += [str(p) for p in sorted(Path(d).rglob("*.class"))]

    dex_out = work_dir / "dex_out"
    dex_out.mkdir(exist_ok=True)

    if class_files or lib_jars:
        await on_progress(65, "🧬 Converting classes → .dex (D8)...")
        d8_tool = R8_JAR if (R8_JAR and os.path.isfile(R8_JAR)) else get_tool(sdk, "d8")
        if R8_JAR and os.path.isfile(R8_JAR):
            dcmd = ["java", "-Xmx4G", "-cp", R8_JAR, "com.android.tools.r8.D8", "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        else:
            dcmd = [d8_tool, "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        await run_tool(dcmd, on_progress, "d8")
        dex_files = sorted(dex_out.glob("*.dex"))

    # Check for prebuilt .dex files in source archive
    prebuilt_dex = [p for p in sorted(extract_dir.rglob("*.dex")) if p.is_file() and not str(p).startswith(str(dex_out))]
    for pd in prebuilt_dex:
        if pd.name not in [d.name for d in dex_files]:
            dex_files.append(pd)

    # C/C++ Native NDK Compilation
    cc_so = {}
    cc_c_files = [f for f in find_inputs(extract_dir, CC_EXTENSIONS) if is_compilable_c_source(f)]
    cc_cpp_files = [f for f in find_inputs(extract_dir, CPP_EXTENSIONS) if is_compilable_c_source(f)]
    lib_name = "native"
    if cc_c_files or cc_cpp_files:
        try:
            main_src = [f for f in cc_c_files + cc_cpp_files if Path(f).stem.lower() == "main"]
            lib_name = Path(main_src[0]).stem if main_src else Path((cc_cpp_files or cc_c_files)[0]).stem
            lib_name = re.sub(r"[^A-Za-z0-9_.-]", "_", lib_name) or "native"
            await on_progress(75, f"⚙️ Compiling native C/C++ libraries (lib{lib_name}.so)...")
            cc_so = await compile_cc_sources(cc_c_files, cc_cpp_files, work_dir, on_progress, lib_name)
        except Exception as ce:
            log.warning("Native C/C++ compilation warning: %s (continuing APK packaging)", ce)

    # Assembling Unsigned APK
    await on_progress(80, "📦 Packaging APK...")
    unsigned_apk = work_dir / "unsigned.apk"
    extra = {}
    for d in dex_files:
        extra[d.name] = d
    for abi, so_path in cc_so.items():
        extra[str(Path("lib") / abi / f"lib{lib_name}.so")] = so_path
    if jni_dirs:
        for j_dir in jni_dirs:
            for so in sorted(j_dir.rglob("*.so")):
                rel = so.relative_to(j_dir)
                extra[str(Path("lib") / rel)] = so

    # Prebuilt .so libraries from archive (collect all native binaries)
    for so in sorted(extract_dir.rglob("*.so")):
        if so.is_file() and not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in so.parts):
            try:
                rel = so.relative_to(extract_dir)
                rel_str = str(rel).replace("\\", "/")
                if not rel_str.startswith("lib/"):
                    parent_name = so.parent.name
                    if parent_name in ("arm64-v8a", "armeabi-v7a", "x86", "x86_64", "armeabi", "mips", "mips64"):
                        arc_path = f"lib/{parent_name}/{so.name}"
                    else:
                        arc_path = f"lib/arm64-v8a/{so.name}"
                else:
                    arc_path = rel_str
                if arc_path not in extra:
                    extra[arc_path] = so
            except Exception:
                pass

    # Prebuilt assets from archive
    for a_dir in sorted(extract_dir.rglob("assets")):
        if a_dir.is_dir() and not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in a_dir.parts):
            for af in sorted(a_dir.rglob("*")):
                if af.is_file():
                    try:
                        rel = af.relative_to(a_dir)
                        arc_path = str(Path("assets") / rel).replace("\\", "/")
                        if arc_path not in extra:
                            extra[arc_path] = af
                    except Exception:
                        pass

    _merge_apk(base_apk, unsigned_apk, extra)
    return await finalize_apk_signing(unsigned_apk, work_dir, on_progress, sdk)


async def finalize_apk_signing(unsigned_apk: Path, work_dir: Path, on_progress, sdk):
    """Zipalign and sign the APK with custom keystore or debug key."""
    global CUSTOM_KEY_ERROR
    zipalign = get_tool(sdk, "zipalign")
    apksigner = get_tool(sdk, "apksigner")

    aligned = work_dir / "aligned.apk"
    if zipalign:
        await on_progress(88, "🔏 Aligning APK (zipalign)...")
        await run_tool([zipalign, "-p", "-f", "4", str(unsigned_apk), str(aligned)], on_progress, "zipalign")
    else:
        shutil.copy2(unsigned_apk, aligned)

    await on_progress(90, "🔏 Signing APK...")
    signed_apk = work_dir / "signed.apk"
    custom_requested = bool(KEYSTORE_JSON and KEYSTORE_JSON.strip())
    try:
        ks_info = get_custom_keystore(work_dir)
    except Exception as e:
        log.warning("get_custom_keystore raised: %s", e)
        CUSTOM_KEY_ERROR = CUSTOM_KEY_ERROR or str(e)[:300]
        ks_info = None
    ks_success = False

    if ks_info and apksigner:
        keystore, storepass, keypass, alias, ks_type = ks_info
        await on_progress(92, f"🔏 Signing with custom key (alias: {alias})...")
        
        # Attempt 1: Standard apksigner command
        sign_cmd = [
            apksigner, "sign", "--ks", str(keystore),
            "--ks-pass", f"pass:{storepass}",
            "--key-pass", f"pass:{keypass}",
            "--ks-key-alias", alias,
            "--v1-signing-enabled", "true",
            "--v2-signing-enabled", "true",
            "--v3-signing-enabled", "true"
        ]
        if ks_type:
            sign_cmd.extend(["--ks-type", ks_type])
        sign_cmd.extend(["--out", str(signed_apk), str(aligned)])
        
        try:
            await run_tool(sign_cmd, on_progress, "apksigner custom key")
            ks_success = True
        except Exception as e1:
            log.warning("Custom keystore signing attempt 1 failed: %s", e1)
            # Attempt 2: Without --ks-type (auto format detection)
            try:
                sign_cmd2 = [
                    apksigner, "sign", "--ks", str(keystore),
                    "--ks-pass", f"pass:{storepass}",
                    "--key-pass", f"pass:{keypass or storepass}",
                    "--ks-key-alias", alias,
                    "--v1-signing-enabled", "true",
                    "--v2-signing-enabled", "true",
                    "--v3-signing-enabled", "true",
                    "--out", str(signed_apk), str(aligned)
                ]
                await run_tool(sign_cmd2, on_progress, "apksigner custom key retry")
                ks_success = True
            except Exception as e2:
                log.warning("Custom keystore signing attempt 2 failed: %s", e2)
                CUSTOM_KEY_ERROR = str(e1)[:250]
                await on_progress(92, "⚠️ Custom key rejected by apksigner. Falling back to debug key...")
    elif custom_requested:
        edit(
            "⚠️ <b>Custom key was requested but could not be used:</b>\n\n"
            f"<code>{CUSTOM_KEY_ERROR or 'Unknown reason'}</code>\n\n"
            "Signing with the <b>debug key</b> instead. Fix the keystore in Settings "
            "(or re-run /setkey) to sign with your own key.",
            parse_mode="HTML", keep_button=False
        )
        await on_progress(92, "⚠️ Custom key unavailable — using debug key...")

    if not ks_success and apksigner:
        keystore = await asyncio.to_thread(make_keystore, work_dir / "debug.keystore")
        sign_cmd = [
            apksigner, "sign", "--ks", str(keystore),
            "--ks-pass", "pass:android",
            "--key-pass", "pass:android",
            "--v1-signing-enabled", "true",
            "--v2-signing-enabled", "true",
            "--v3-signing-enabled", "true",
            "--out", str(signed_apk), str(aligned)
        ]
        await run_tool(sign_cmd, on_progress, "apksigner debug key")
    elif not apksigner:
        shutil.copy2(aligned, signed_apk)

    if apksigner and signed_apk.exists():
        try:
            await run_tool([apksigner, "verify", str(signed_apk)], on_progress, "apksigner verify")
        except Exception as ve:
            log.warning("apksigner verify notice: %s", ve)

    await on_progress(98, "✅ APK successfully built & signed!")
    return signed_apk, unsigned_apk


class TolerantZipFile(zipfile.ZipFile):
    def _RealGetContents(self):
        super()._RealGetContents()
        for zinfo in self.filelist:
            zinfo._end_offset = None


def make_keystore(path: Path) -> Path:
    if not path.exists():
        subprocess.run(
            ["keytool", "-genkeypair", "-keystore", str(path), "-storepass", "android", "-keypass", "android",
             "-alias", "androiddebugkey", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=Android Debug,O=Android,C=US"],
            check=True, capture_output=True
        )
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
        return "", [], False

    m = re.search(r"Keystore type:\s*([A-Za-z0-9_]+)", out, re.I)
    ks_type = m.group(1).lower() if m else ""
    aliases = []
    for line in out.splitlines():
        al = re.match(r"^Alias name:\s*(.+)$", line.strip(), re.I)
        if al:
            aliases.append(al.group(1).strip())
    if proc.returncode != 0:
        if "password" in out.lower() or "tampered" in out.lower() or "integrity" in out.lower():
            return ks_type, aliases, False
    return ks_type, aliases, True


def get_custom_keystore(work_dir: Path):
    """Return (keystore_path, storepass, keypass, alias, ks_type) or None.

    Never raises: on any problem it records a human-readable reason in the
    module-level CUSTOM_KEY_ERROR and returns None, so signing falls back to
    the debug key instead of killing the whole job with a traceback.
    """
    global CUSTOM_KEY_ERROR
    CUSTOM_KEY_ERROR = ""
    if not KEYSTORE_JSON or not KEYSTORE_JSON.strip():
        return None
    try:
        data = json.loads(KEYSTORE_JSON)
        b64 = (data.get("keystore_b64") or "").strip()
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
        storepass = (data.get("storepass") or "").strip() or "android"
        keypass = (data.get("keypass") or "").strip() or storepass
        alias = (data.get("alias") or "").strip()
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


def is_compilable_c_source(file_path: str) -> bool:
    """Filter out Ghidra pseudo-C decompiler outputs that cannot be compiled directly with Clang.

    Ghidra exports C with decompiler-only constructs (undefined8, FUN_00123456, PTR_LOOP_*,
    DAT_*, param_1, __cxa_finalize stubs, (code *) casts...). Real NDK C/C++ never contains
    these, so a weighted scan of the whole file reliably separates real native sources from
    decompiler pseudo-code.
    """
    try:
        p = Path(file_path)
        if any(part in ("ghidra", "decompile", "decompiled", "pseudocode") for part in p.parts):
            return False
        content = p.read_text(encoding="utf-8", errors="replace")[:200000]
        strong_markers = (
            "undefined8", "undefined4", "undefined2", "undefined1",
            "FUN_", "PTR_LOOP_", "PTR_DAT_", "extraout_",
            "__thiscall", "(code *)", "UNRECOVERED_JUMPTABLE",
            "__cxa_finalize", "__register_atfork", "__cxa_atexit",
            "CONCAT44(", "CONCAT11(", "SUB84(", "SUB42(",
        )
        weak_markers = ("undefined", "code *", "param_1", "ulong", "longlong", "DAT_", "LAB_")
        strong = sum(content.count(m) for m in strong_markers)
        weak = sum(content.count(m) for m in weak_markers)
        if strong >= 2 or (strong >= 1 and weak >= 2):
            log.info("Skipping Ghidra decompiler pseudo-C from Clang: %s (strong=%d, weak=%d)", p.name, strong, weak)
            return False
        return True
    except Exception:
        return True


def find_clang_for(ndk_bin: str, abi: str):
    for base in ABI_CLANG_NAMES.get(abi, []):
        c = os.path.join(ndk_bin, base)
        if os.path.exists(c):
            return c, os.path.join(ndk_bin, base + "++")
    return "", ""


async def compile_cc_sources(c_files: list, cpp_files: list, work_dir: Path, on_progress, lib_name: str) -> dict:
    ndk_bin = find_ndk_bin()
    if not ndk_bin:
        log.warning("Android NDK not found on runner; skipping native C/C++ compilation")
        return {}

    # Filter out pseudo-C decompiler outputs
    c_files = [f for f in c_files if is_compilable_c_source(f)]
    cpp_files = [f for f in cpp_files if is_compilable_c_source(f)]

    if not c_files and not cpp_files:
        log.info("No raw compilable C/C++ source files to build with Clang.")
        return {}

    results = {}
    for abi in ABI_CLANG_NAMES:
        try:
            clang, clangxx = find_clang_for(ndk_bin, abi)
            if not clang:
                continue
            out_so = work_dir / f"lib_{abi}_{lib_name}.so"
            await on_progress(75, f"⚙️ Compiling C/C++ → {abi} .so...")
            if c_files and not cpp_files:
                await run_tool([clang, "-shared", "-fPIC", "-O2", "-std=c11", "-o", str(out_so)] + c_files, on_progress, f"clang {abi}")
            elif cpp_files and not c_files:
                await run_tool([clangxx, "-shared", "-fPIC", "-O2", "-std=c++17", "-o", str(out_so)] + cpp_files, on_progress, f"clang++ {abi}")
            else:
                obj_dir = work_dir / f"obj_{abi}"
                obj_dir.mkdir(exist_ok=True)
                objs = []
                for i, f in enumerate(c_files):
                    obj = obj_dir / f"c_{i}.o"
                    await run_tool([clang, "-c", "-fPIC", "-O2", "-std=c11", "-o", str(obj), f], on_progress, f"clang {abi}")
                    if obj.exists():
                        objs.append(str(obj))
                for i, f in enumerate(cpp_files):
                    obj = obj_dir / f"cpp_{i}.o"
                    await run_tool([clangxx, "-c", "-fPIC", "-O2", "-std=c++17", "-o", str(obj), f], on_progress, f"clang++ {abi}")
                    if obj.exists():
                        objs.append(str(obj))
                if objs:
                    await run_tool([clangxx, "-shared", "-O2", "-o", str(out_so)] + objs, on_progress, f"link {abi}")
            if out_so.exists():
                results[abi] = out_so
        except Exception as e:
            log.warning("C/C++ compilation warning for %s: %s (continuing APK packaging)", abi, e)
    return results


def _merge_apk(base_apk: Path, out_apk: Path, extra: dict):
    norm_extra = {}
    for arc, src in extra.items():
        k = str(arc).replace("\\", "/").lstrip("/")
        norm_extra[k] = src

    written_entries = set()
    with TolerantZipFile(base_apk) as zin:
        with zipfile.ZipFile(out_apk, "w") as zout:
            for item in zin.infolist():
                entry_name = item.filename.replace("\\", "/").lstrip("/")
                if entry_name in written_entries:
                    continue
                # If extra contains an overriding version, write that instead
                if entry_name in norm_extra:
                    src = norm_extra.pop(entry_name)
                    c_type = zipfile.ZIP_STORED if (entry_name.endswith(".so") or entry_name == "resources.arsc") else zipfile.ZIP_DEFLATED
                    zout.write(str(src), entry_name, compress_type=c_type)
                    written_entries.add(entry_name)
                else:
                    c_type = item.compress_type
                    if entry_name.endswith(".so") or entry_name == "resources.arsc":
                        c_type = zipfile.ZIP_STORED
                    zout.writestr(item, zin.read(item.filename), compress_type=c_type)
                    written_entries.add(entry_name)

            for entry_name, src in norm_extra.items():
                if entry_name in written_entries:
                    continue
                c_type = zipfile.ZIP_STORED if (entry_name.endswith(".so") or entry_name == "resources.arsc") else zipfile.ZIP_DEFLATED
                zout.write(str(src), entry_name, compress_type=c_type)
                written_entries.add(entry_name)


async def upload_document(path: Path, caption: str):
    http_ok = False
    if path.stat().st_size <= 50 * 1024 * 1024:
        try:
            with open(path, "rb") as doc_f:
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
            sys.executable, "upload_file.py", str(path), caption,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await proc.wait()
        if proc.returncode != 0:
            raise ValueError(f"MTProto Upload failed with code {proc.returncode}")


async def poll_cancel_commands():
    if not JOB_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"https://ntfy.sh/{JOB_ID}_cmd/raw") as response:
                async for line in response.aiter_lines():
                    if "cancel" in line.lower() or "stop" in line.lower():
                        log.info("Cancellation received from user via app/notification!")
                        CANCELLED["v"] = True
                        break
    except Exception:
        pass


async def main():
    if not JOB_ID and (not BOT_TOKEN or not CHAT_ID):
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    cancel_task = asyncio.create_task(poll_cancel_commands())
    edit("🟢 Job started! Preparing APK Build engine on cloud runner...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("apkbuild_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        global TOOL_LOG_FH
        TOOL_LOG_FH = open(work_dir / "build_log.txt", "a", encoding="utf-8", errors="replace")
        ext = Path(FILENAME).suffix or ".zip"
        dest = work_dir / f"input_file{ext}"
        last = [-100.0]

        dl_method = ["📥 Downloading file..."]
        last_dl_time = [0.0]

        async def on_dl(pct: float):
            now = time.time()
            if pct < 100.0 and (pct < last[0] or ((pct - last[0] < 5.0) and (now - last_dl_time[0] < 3.0))):
                return
            last[0] = pct
            last_dl_time[0] = now
            edit(f"{dl_method[0]}\n\n{progress_bar(pct)}")

        # Download input file
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
                    if dest.exists() and dest.stat().st_size > 0:
                        got_file = True
                except Exception as http_err:
                    if not file_id:
                        raise
                    log.warning("HTTP download failed, falling back to MTProto: %s", http_err)

            if not got_file and file_id:
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
                    if not FILE_URL:
                        raise ValueError(f"MTProto Download failed with code {proc.returncode}: {err_tail}")
                    log.warning("MTProto download failed (falling back to FILE_URL): %s", err_tail)
                elif dest.exists() and dest.stat().st_size > 0:
                    got_file = True

            if not got_file and FILE_URL:
                dl_method[0] = "📥 Downloading source archive..."
                await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
                if dest.exists() and dest.stat().st_size > 0:
                    got_file = True
        except Exception as e:
            await send_error_log(work_dir, e, "Download failed")
            return

        if not dest.exists() or dest.stat().st_size == 0:
            edit("❌ Downloaded file is empty (0 bytes).", keep_button=False)
            return

        size = dest.stat().st_size
        if size > MAX_DOWNLOAD_MB * 1024 * 1024 and not IS_ADMIN:
            edit(f"❌ File is {size/1024/1024:.1f} MB — max download limit is {MAX_DOWNLOAD_MB} MB.", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing APK build...")

        sdk = find_sdk()
        if not sdk["build_tools"] or not sdk["platforms"]:
            edit("❌ Android SDK build-tools/platforms not found on runner.", keep_button=False)
            return

        last_prog = [0, ""]

        async def on_progress(pct: int, label: str = "📦 Building APK..."):
            if pct - last_prog[0] < 4 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            signed_apk, unsigned_apk = await build_apk_from_source(dest, work_dir, on_progress, sdk)
            done_msg = "✅ APK build successfully completed!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The project is too large to build within limit.", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "APK Build failed")
            return

        await on_progress(100, done_msg)

        if JOB_ID:
            upload_result_for_app(signed_apk)

        if BOT_TOKEN and BOT_TOKEN != "app_direct_mode" and CHAT_ID and CHAT_ID != "me":
            try:
                await upload_document(signed_apk, "✅ <b>Signed APK</b> built from source — Powered By @R3V_X")
                if unsigned_apk and unsigned_apk.exists():
                    edit("📤 Sending unsigned APK...")
                    await upload_document(unsigned_apk, "✅ <b>Unsigned APK</b> built from source — Powered By @R3V_X")
                edit("✅ APK build complete! Signed + Unsigned delivered. 🔥", keep_button=False)
            except Exception as e:
                log.warning("Telegram upload failed: %s", e)
                if not JOB_ID:
                    await send_error_log(work_dir, e, "Result upload failed")
        else:
            edit("✅ APK build complete! Ready for download.", keep_button=False)
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
