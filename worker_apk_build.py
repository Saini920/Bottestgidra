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
R8_JAR = os.environ.get("PAYLOAD_R8_JAR", "")
KOTLINC_ROOT = os.environ.get("PAYLOAD_KOTLINC", "")
KEYSTORE_JSON = os.environ.get("PAYLOAD_CUSTOM_KEYSTORE_JSON") or os.environ.get("PAYLOAD_KEYSTORE", "")
MAX_DOWNLOAD_MB = 2000 if IS_ADMIN else 500

JAVA_EXTENSIONS = {".java"}
KOTLIN_EXTENSIONS = {".kt"}
MAX_SRC_FILES_FREE = 50
MAX_SRC_FILES_PREMIUM = 200
MAX_CC_FILES = 200

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


async def send_error_log(work_dir, exception_obj, title="APK Build failed"):
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
                await on_progress(100)
                return filename
        raise ValueError("Could not download file from this link.")


async def run_tool(cmd: list, on_progress, label: str, timeout: int = 86400, progress_stall: int = 1800, cwd: str = None):
    log.info("Running: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=cwd
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


def find_inputs(src_dir: Path, suffixes) -> list:
    return [str(p) for p in sorted(Path(src_dir).rglob("*")) if p.is_file() and p.suffix.lower() in suffixes]


def find_dir(src_dir: Path, names) -> Path:
    for n in names:
        for cand in sorted(src_dir.rglob(n)):
            if cand.is_dir():
                return cand
    return None


def kotlinc_bin():
    if not KOTLINC_ROOT:
        return ""
    for cand in (os.path.join(KOTLINC_ROOT, "bin", "kotlinc"),
                 os.path.join(KOTLINC_ROOT, "kotlinc")):
        if os.path.isfile(cand):
            return cand
    return ""


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
    platforms = []
    pl_dir = os.path.join(root, "platforms")
    if os.path.isdir(pl_dir):
        platforms = sorted(
            (d for d in os.listdir(pl_dir) if re.match(r"^android-\d+$", d)),
            key=lambda v: int(v.split("-")[1]), reverse=True)
    return {"root": root, "build_tools": build_tools, "platforms": platforms}


def get_tool(sdk, name):
    for v in sdk["build_tools"]:
        p = os.path.join(sdk["root"], "build-tools", v, name)
        if os.path.exists(p):
            return p
    return ""


def get_android_jar(sdk):
    for p in sdk["platforms"]:
        pj = os.path.join(sdk["root"], "platforms", p, "android.jar")
        if os.path.exists(pj):
            return pj, int(p.split("-")[1])
    return "", 0


def parse_manifest(path: Path):
    text = path.read_text(errors="replace")
    package = ""
    m = re.search(r'package="([^"]+)"', text)
    if m:
        package = m.group(1)
    min_sdk = None
    m = re.search(r'<uses-sdk[^>]*android:minSdkVersion\s*=\s*"(\d+)"', text)
    if m:
        min_sdk = int(m.group(1))
    target_sdk = None
    m = re.search(r'<uses-sdk[^>]*android:targetSdkVersion\s*=\s*"(\d+)"', text)
    if m:
        target_sdk = int(m.group(1))
    version_code = None
    m = re.search(r'android:versionCode\s*=\s*"(\d+)"', text)
    if m:
        version_code = int(m.group(1))
    version_name = None
    m = re.search(r'android:versionName\s*=\s*"([^"]+)"', text)
    if m:
        version_name = m.group(1)
    return {"package": package, "min_sdk": min_sdk, "target_sdk": target_sdk,
            "version_code": version_code, "version_name": version_name}


def ensure_manifest_package(manifest: Path, extract_dir: Path) -> str:
    text = manifest.read_text(errors="replace")
    m = re.search(r"<manifest[^>]*\bpackage\s*=\s*[\"']([^\"']+)[\"']", text)
    if m:
        return m.group(1)
    pkg = ""
    for gp in sorted(extract_dir.rglob("build.gradle")) + sorted(extract_dir.rglob("build.gradle.kts")):
        try:
            gt = gp.read_text(errors="replace")
        except Exception:
            continue
        gm = (re.search(r"namespace\s*=\s*\"([^\"]+)\"", gt)
              or re.search(r"applicationId\s*=\s*\"([^\"]+)\"", gt)
              or re.search(r"applicationId\s+\"([^\"]+)\"", gt)
              or re.search(r"namespace\s+\"([^\"]+)\"", gt))
        if gm:
            pkg = gm.group(1)
            break
    if not pkg:
        pkg = "com.example.app"
    text = text.replace("<manifest", f'<manifest package="{pkg}"', 1)
    manifest.write_text(text)
    return pkg


async def build_apk_from_source(input_path: Path, work_dir: Path, on_progress, sdk):
    await on_progress(15, "📦 Extracting source code...")
    extract_dir = work_dir / "src"
    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(input_path, "r") as zf:
        zf.extractall(extract_dir)

    build_mode = os.environ.get("PAYLOAD_BUILD_MODE", "")
    
    gradlew = None
    build_gradle = None
    for g in sorted(extract_dir.rglob("gradlew")):
        if g.is_file():
            gradlew = g
            break
            
    for b in sorted(extract_dir.rglob("build.gradle")) + sorted(extract_dir.rglob("build.gradle.kts")):
        if b.is_file():
            build_gradle = b
            break
            
    has_gradle = bool(gradlew) or bool(build_gradle)
    
    if build_mode == "manifest":
        pass
    elif build_mode == "gradle":
        build_mode = "gradle"
    elif build_mode in ("auto", ""):
        build_mode = "gradle" if has_gradle else "manifest"
    else:
        build_mode = "gradle" if has_gradle else "manifest"

    if build_mode == "gradle":
        await on_progress(20, "🚀 Building using Gradle...")
        if gradlew:
            target_dir = gradlew.parent
        elif build_gradle:
            # If no gradlew, use the root project folder that contains build.gradle
            # It's better to find settings.gradle to ensure it's the root project
            settings = [p for p in extract_dir.rglob("settings.gradle*") if p.is_file()]
            if settings:
                target_dir = settings[0].parent
            else:
                target_dir = build_gradle.parent
        else:
            target_dir = extract_dir
        
        if not gradlew:
            try:
                await on_progress(22, "⚙️ Setting up Gradle wrapper (8.10.2)...")
                await run_tool(["gradle", "wrapper", "--gradle-version", "8.10.2"], on_progress, "gradle wrapper", cwd=str(target_dir))
                cand = target_dir / "gradlew"
                if cand.is_file():
                    gradlew = cand
            except Exception as e:
                log.warning("gradle wrapper generation failed: %s", e)

        cmd = []
        if gradlew:
            os.chmod(gradlew, 0o755)
            cmd = ["./gradlew", "assembleDebug", "--no-daemon"]
        else:
            cmd = ["gradle", "assembleDebug", "--no-daemon"]
            
        try:
            await on_progress(28, "🚀 Compiling APK with Gradle...")
            await run_tool(cmd, on_progress, "gradle", cwd=str(target_dir))
        except Exception as e:
            raise ValueError(f"Gradle build failed: {e}")
            
        apks = sorted(target_dir.rglob("*.apk"))
        if not apks:
            raise ValueError("Gradle build completed but no .apk files were found in the project.")
        found_apk = apks[-1]
        
        unsigned_apk = work_dir / "unsigned.apk"
        import shutil
        shutil.copy2(found_apk, unsigned_apk)

        zipalign = get_tool(sdk, "zipalign")
        apksigner = get_tool(sdk, "apksigner")

        aligned = work_dir / "aligned.apk"
        if zipalign:
            await on_progress(88, "🔏 Aligning APK (zipalign)...")
            await run_tool([zipalign, "-p", "-f", "4", str(found_apk), str(aligned)], on_progress, "zipalign")
        else:
            shutil.copy2(found_apk, aligned)

        await on_progress(90, "🔏 Signing APK...")
        signed_apk = work_dir / "signed.apk"
        ks_info = get_custom_keystore(work_dir)
        ks_success = False
        if ks_info and apksigner:
            keystore, storepass, keypass, alias, ks_type = ks_info
            await on_progress(90, f"🔏 Using your custom signing key (alias: {alias})...")
            sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", f"pass:{storepass}",
                        "--key-pass", f"pass:{keypass}", "--ks-key-alias", alias,
                        "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true"]
            if ks_type:
                sign_cmd.extend(["--ks-type", ks_type])
            sign_cmd.extend(["--out", str(signed_apk), str(aligned)])
            try:
                await run_tool(sign_cmd, on_progress, "apksigner")
                ks_success = True
            except Exception as e:
                log.warning("Custom keystore signing failed: %s", e)
                await on_progress(90, "⚠️ Custom keystore failed. Falling back to debug key...")
                ks_success = False

        if not ks_success and apksigner:
            keystore = await asyncio.to_thread(make_keystore, work_dir / "debug.keystore")
            sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", "pass:android", "--key-pass", "pass:android",
                        "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
                        "--out", str(signed_apk), str(aligned)]
            await run_tool(sign_cmd, on_progress, "apksigner")
        elif not apksigner:
            shutil.copy2(found_apk, signed_apk)

        if apksigner and signed_apk.exists():
            await run_tool([apksigner, "verify", str(signed_apk)], on_progress, "apksigner verify")

        await on_progress(95, "✅ APK built and signed!")
        return signed_apk, unsigned_apk


    manifest = extract_dir / "AndroidManifest.xml"
    if not manifest.exists():
        m2 = [p for p in extract_dir.rglob("*") if p.is_file() and p.name.lower() == "androidmanifest.xml"]
        if m2:
            manifest = m2[0]
        else:
            # Auto-generate a standard AndroidManifest.xml if not found in the ZIP
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
        # If inside any 'assets' folder, it belongs to assets!
        if any(part.lower() == "assets" for part in r_path.parts):
            return False
        if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in r_path.parts):
            return False
        subdirs = [p.name.lower() for p in r_path.iterdir() if p.is_dir()]
        if not subdirs:
            return any(p.suffix.lower() in (".xml", ".png", ".jpg", ".webp", ".9.png") for p in r_path.iterdir() if p.is_file())
        for s in subdirs:
            base_type = s.split("-")[0]
            if base_type in VALID_RES_PREFIXES:
                return True
        return False

    res_dirs = []
    for r in sorted(extract_dir.rglob("res")):
        if is_valid_android_res_dir(r):
            res_dirs.append(r)

    if not res_dirs:
        dummy_res = work_dir / "default_res"
        (dummy_res / "values").mkdir(parents=True, exist_ok=True)
        (dummy_res / "values" / "strings.xml").write_text('<resources><string name="app_name">App</string></resources>', encoding="utf-8")
        res_dirs.append(dummy_res)

    assets_dirs = []
    for a in sorted(extract_dir.rglob("assets")):
        if a.is_dir() and any(a.iterdir()):
            if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in a.parts):
                continue
            if not any(a != ex and str(a).startswith(str(ex)) for ex in assets_dirs):
                assets_dirs.append(a)

    jni_dirs = []
    for j in sorted(extract_dir.rglob("jniLibs")) + sorted(extract_dir.rglob("libs")):
        if j.is_dir() and any(j.rglob("*.so")):
            if not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in j.parts):
                jni_dirs.append(j)

    libs_dirs = []
    for l in sorted(extract_dir.rglob("libs")):
        if l.is_dir() and any(l.rglob("*.jar")):
            if not any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in l.parts):
                libs_dirs.append(l)

    java_files = []
    kt_files = []
    for p in sorted(extract_dir.rglob("*")):
        if p.is_file():
            if any(part in ("build", ".gradle", "bin", "out", "target", ".idea", ".git") for part in p.parts):
                continue
            is_test = False
            for idx, part in enumerate(p.parts):
                if part == "src" and idx + 1 < len(p.parts) and p.parts[idx+1] in ("test", "androidTest"):
                    is_test = True
                    break
            if is_test:
                continue
            
            if p.suffix.lower() in JAVA_EXTENSIONS:
                java_files.append(str(p))
            elif p.suffix.lower() in KOTLIN_EXTENSIONS:
                kt_files.append(str(p))
    java_files = sorted(set(java_files))
    kt_files = sorted(set(kt_files))

    aapt2 = get_tool(sdk, "aapt2")
    zipalign = get_tool(sdk, "zipalign")
    apksigner = get_tool(sdk, "apksigner")
    android_jar, compile_api = get_android_jar(sdk)
    if not aapt2 or not zipalign or not apksigner:
        raise ValueError("Android SDK build-tools missing (aapt2/zipalign/apksigner required).")
    if not android_jar:
        raise ValueError("No Android platform (android.jar) found in the SDK.")

    target_sdk = mp["target_sdk"] or (compile_api or 34)
    min_sdk = mp["min_sdk"] or 4
    package = mp["package"] or "com.example.app"

    await on_progress(25, "📦 Compiling resources (aapt2)...")
    compiled_res_list = []
    for idx, r_dir in enumerate(res_dirs):
        c_res = work_dir / f"compiled_res_{idx}.zip"
        try:
            await run_tool([aapt2, "compile", "--dir", str(r_dir), "-o", str(c_res)], on_progress, f"aapt2 compile res_{idx}")
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

    gen_dir = work_dir / "gen"
    gen_dir.mkdir(exist_ok=True)
    base_apk = work_dir / "base.apk"
    link_cmd = [aapt2, "link", "-o", str(base_apk), "-I", android_jar, "--manifest", str(manifest),
                "--java", str(gen_dir),
                "--min-sdk-version", str(min_sdk),
                "--target-sdk-version", str(target_sdk),
                "--auto-add-overlay"]
    if mp["version_code"]:
        link_cmd += ["--version-code", str(mp["version_code"])]
    if mp["version_name"]:
        link_cmd += ["--version-name", mp["version_name"]]
    for a_dir in assets_dirs:
        link_cmd += ["-A", str(a_dir)]
    for c_res in compiled_res_list:
        link_cmd += [c_res]
    await run_tool(link_cmd, on_progress, "aapt2 link")
    if not base_apk.exists():
        raise ValueError("aapt2 link produced no APK (check AndroidManifest.xml).")

    class_dirs = []
    cp_parts = [android_jar]
    lib_jars = []
    if libs_dirs:
        for l_dir in libs_dirs:
            lib_jars += find_inputs(l_dir, {".jar"})
        cp_parts += lib_jars

    if kt_files:
        kbin = kotlinc_bin()
        if not kbin:
            raise ValueError("Kotlin compiler not found (send Java-only source or fix runner).")
        kotlin_out = work_dir / "kotlin_out"
        kotlin_out.mkdir(exist_ok=True)
        await on_progress(40, "☕ Compiling Kotlin sources...")
        kcmd = ["bash", kbin, "-classpath", android_jar, "-jvm-target", "1.8", "-d", str(kotlin_out)] + kt_files
        await run_tool(kcmd, on_progress, "kotlinc")
        class_dirs.append(kotlin_out)
        cp_parts.append(str(kotlin_out))
        stdlib = os.path.join(KOTLINC_ROOT, "lib", "kotlin-stdlib.jar")
        if os.path.isfile(stdlib):
            cp_parts.append(stdlib)
            lib_jars.append(stdlib)
        for extra in ("kotlin-stdlib-jdk8.jar", "kotlin-stdlib-jdk7.jar"):
            p = os.path.join(KOTLINC_ROOT, "lib", extra)
            if os.path.isfile(p):
                lib_jars.append(p)

    r_java = find_inputs(gen_dir, JAVA_EXTENSIONS)
    if java_files or r_java:
        java_out = work_dir / "java_out"
        java_out.mkdir(exist_ok=True)
        await on_progress(50, "☕ Compiling Java sources...")
        jcmd = ["javac", "-encoding", "UTF-8", "-classpath", ":".join(cp_parts), "-d", str(java_out)] + r_java + java_files
        await run_tool(jcmd, on_progress, "javac")
        class_dirs.append(java_out)
        if not any(java_out.rglob("*.class")) and not any(kotlin_out.rglob("*.class") if 'kotlin_out' in dir() else []):
            raise ValueError("No .class files generated (check your Java/Kotlin source).")

    dex_files = []
    class_files = []
    for d in class_dirs:
        class_files += [str(p) for p in sorted(Path(d).rglob("*.class"))]
    if class_files:
        await on_progress(65, "🧬 Converting classes → .dex (D8)...")
        dex_out = work_dir / "dex_out"
        dex_out.mkdir(exist_ok=True)
        if R8_JAR and os.path.isfile(R8_JAR):
            dcmd = ["java", "-Xmx4G", "-cp", R8_JAR, "com.android.tools.r8.D8", "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        else:
            d8 = get_tool(sdk, "d8")
            if not d8:
                raise ValueError("d8 not found (need PAYLOAD_R8_JAR or build-tools d8).")
            dcmd = [d8, "--release", "--min-api", str(min_sdk), "--lib", android_jar, "--output", str(dex_out)] + class_files + lib_jars
        await run_tool(dcmd, on_progress, "d8")
        dex_files = sorted(dex_out.glob("*.dex"))
        if not dex_files:
            raise ValueError("d8 produced no .dex output.")

    cc_so = {}
    cc_c_files = find_inputs(extract_dir, CC_EXTENSIONS)
    cc_cpp_files = find_inputs(extract_dir, CPP_EXTENSIONS)
    if cc_c_files or cc_cpp_files:
        main_src = [f for f in cc_c_files + cc_cpp_files if Path(f).stem.lower() == "main"]
        if main_src:
            lib_name = "main"
        else:
            first = (cc_cpp_files or cc_c_files)[0]
            lib_name = Path(first).stem
        lib_name = re.sub(r"[^A-Za-z0-9_.-]", "_", lib_name) or "native"
        await on_progress(75, "⚙️ Compiling C/C++ sources (NDK)...")
        cc_so = await compile_cc_sources(cc_c_files, cc_cpp_files, work_dir, on_progress, lib_name)
        if not cc_so:
            raise ValueError("C/C++ sources found but no .so could be compiled (NDK clang missing).")

    await on_progress(80, "📦 Assembling APK...")
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
    _merge_apk(base_apk, unsigned_apk, extra)

    aligned = work_dir / "aligned.apk"
    await run_tool([zipalign, "-p", "-f", "4", str(unsigned_apk), str(aligned)], on_progress, "zipalign")
    await on_progress(88, "🔏 Signing APK...")
    signed_apk = work_dir / "signed.apk"
    ks_info = get_custom_keystore(work_dir)
    ks_success = False
    if ks_info:
        keystore, storepass, keypass, alias, ks_type = ks_info
        await on_progress(88, "🔏 Using your custom signing key...")
        sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", f"pass:{storepass}",
                    "--key-pass", f"pass:{keypass}", "--ks-key-alias", alias,
                    "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true"]
        if min_sdk:
            sign_cmd.extend(["--min-sdk-version", str(min_sdk)])
        if ks_type:
            sign_cmd.extend(["--ks-type", ks_type])
        sign_cmd.extend(["--out", str(signed_apk), str(aligned)])
        try:
            await run_tool(sign_cmd, on_progress, "apksigner")
            ks_success = True
        except Exception as e:
            log.warning("Custom keystore signing failed: %s", e)
            await on_progress(88, "⚠️ Custom keystore failed (Wrong Password?). Falling back to debug key...")
            ks_success = False

    if not ks_success:
        keystore = await asyncio.to_thread(make_keystore, work_dir / "debug.keystore")
        sign_cmd = [apksigner, "sign", "--ks", str(keystore), "--ks-pass", "pass:android", "--key-pass", "pass:android",
                    "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true"]
        if min_sdk:
            sign_cmd.extend(["--min-sdk-version", str(min_sdk)])
        sign_cmd.extend(["--out", str(signed_apk), str(aligned)])
        await run_tool(sign_cmd, on_progress, "apksigner")
        
    await run_tool([apksigner, "verify", str(signed_apk)], on_progress, "apksigner verify")
    await on_progress(95, "✅ APK built!")
    return signed_apk, aligned


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
    import subprocess as sp
    if not path.exists():
        sp.run(["keytool", "-genkeypair", "-keystore", str(path), "-storepass", "android", "-keypass", "android",
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
    if not KEYSTORE_JSON or not KEYSTORE_JSON.strip():
        return None
    try:
        info = json.loads(KEYSTORE_JSON)
        b64 = info.get("keystore_b64") or info.get("b64") or ""
        if not b64:
            return None
        ks_path = work_dir / "custom.keystore"
        ks_path.write_bytes(base64.b64decode(b64))
        if not ks_path.exists() or ks_path.stat().st_size == 0:
            return None
        storepass = info.get("storepass", "android")
        keypass = info.get("keypass", storepass)
        alias = (info.get("alias") or "").strip()
        ks_type, aliases, ok = inspect_custom_keystore(ks_path, storepass)
        if not ok:
            raise ValueError(
                "Custom signing key: keystore password (storepass) is incorrect. "
                "Run /setkey again with the correct storepass."
            )
        if alias and alias not in aliases and aliases:
            alias = aliases[0]
        if not alias and aliases:
            alias = aliases[0]
        return ks_path, storepass, keypass, alias or "androiddebugkey", ks_type
    except ValueError:
        raise
    except Exception as e:
        log.warning("Failed to parse custom keystore: %s", e)
        return None


def find_clang_for(ndk_bin: str, abi: str):
    for base in ABI_CLANG_NAMES.get(abi, []):
        c = os.path.join(ndk_bin, base)
        if os.path.exists(c):
            return c, os.path.join(ndk_bin, base + "++")
    return "", ""


async def compile_cc_sources(c_files: list, cpp_files: list, work_dir: Path, on_progress, lib_name: str) -> dict:
    ndk_bin = find_ndk_bin()
    if not ndk_bin:
        raise ValueError("Android NDK not found on the runner (C/C++ sources present).")
    results = {}
    for abi in ABI_CLANG_NAMES:
        clang, clangxx = find_clang_for(ndk_bin, abi)
        if not clang:
            log.warning("NDK clang for %s not found, skipping", abi)
            continue
        out_so = work_dir / f"lib_{abi}_{lib_name}.so"
        await on_progress(0, f"⚙️ Compiling C/C++ → {abi} .so...")
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
                if not obj.exists():
                    raise RuntimeError(f"clang produced no object file for {f}")
                objs.append(str(obj))
            for i, f in enumerate(cpp_files):
                obj = obj_dir / f"cpp_{i}.o"
                await run_tool([clangxx, "-c", "-fPIC", "-O2", "-std=c++17", "-o", str(obj), f], on_progress, f"clang++ {abi}")
                if not obj.exists():
                    raise RuntimeError(f"clang++ produced no object file for {f}")
                objs.append(str(obj))
            if not objs:
                raise ValueError("No object files produced from C/C++ sources.")
            await run_tool([clangxx, "-shared", "-O2", "-o", str(out_so)] + objs, on_progress, f"link {abi}")
        if out_so.exists():
            results[abi] = out_so
    return results


class TolerantZipFile(zipfile.ZipFile):
    def _RealGetContents(self):
        super()._RealGetContents()
        for zinfo in self.filelist:
            zinfo._end_offset = None


def _merge_apk(base_apk: Path, out_apk: Path, extra: dict):
    with TolerantZipFile(base_apk) as zin:
        with zipfile.ZipFile(out_apk, "w") as zout:
            for item in zin.infolist():
                c_type = item.compress_type
                if item.filename.endswith(".so") or item.filename == "resources.arsc":
                    c_type = zipfile.ZIP_STORED
                zout.writestr(item, zin.read(item.filename), compress_type=c_type)
            for arc, src in extra.items():
                arc_str = str(arc)
                c_type = zipfile.ZIP_DEFLATED
                if arc_str.endswith(".so") or arc_str == "resources.arsc":
                    c_type = zipfile.ZIP_STORED
                zout.write(str(src), arc_str, compress_type=c_type)


def check_zip_limits(file_path: Path):
    return


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
    except Exception as e:
        pass


async def main():
    if not JOB_ID and (not BOT_TOKEN or not CHAT_ID):
        log.error("Missing env TELEGRAM_BOT_TOKEN / PAYLOAD_CHAT_ID")
        sys.exit(1)

    cancel_task = asyncio.create_task(poll_cancel_commands())

    edit("🟢 Job started! Preparing APK Build engine on cloud server...", parse_mode="HTML")

    work_dir = Path(tempfile.gettempdir()) / ("apkbuild_" + os.urandom(8).hex())
    try:
        work_dir.mkdir(parents=True)
        global TOOL_LOG_FH
        TOOL_LOG_FH = open(work_dir / "build_log.txt", "a", encoding="utf-8", errors="replace")
        ext = Path(FILENAME).suffix or ".bin"
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
                    if dest.exists() and dest.stat().st_size > 0:
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
                if dest.exists() and dest.stat().st_size > 0:
                    got_file = True

            if not got_file and FILE_URL:
                filename = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
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

        try:
            check_zip_limits(dest)
        except ValueError as e:
            edit(f"❌ {e}", keep_button=False)
            return

        edit(f"📥 Downloaded {size/1024/1024:.1f} MB! Preparing APK build...")

        sdk = find_sdk()
        if not sdk["build_tools"] or not sdk["platforms"]:
            edit("❌ Android SDK build-tools/platforms not found on the runner.", keep_button=False)
            return

        last_prog = [0, ""]
        async def on_progress(pct: int, label: str = "📦 Building APK..."):
            if pct - last_prog[0] < 5 and label == last_prog[1]:
                return
            last_prog[0], last_prog[1] = pct, label
            edit(f"{label}\n{progress_bar(pct)}")

        try:
            signed_apk, unsigned_apk = await build_apk_from_source(dest, work_dir, on_progress, sdk)
            done_msg = "✅ APK build complete!"
        except asyncio.TimeoutError:
            edit("⏰ Timeout! The source is too large to build.", keep_button=False)
            return
        except Exception as e:
            await send_error_log(work_dir, e, "APK Build failed")
            return

        await on_progress(100, done_msg)
        if JOB_ID:
            upload_result_for_app(signed_apk)

        elif BOT_TOKEN and BOT_TOKEN != "app_direct_mode" and CHAT_ID:
            try:
                await upload_document(signed_apk, f"✅ <b>Signed APK</b> built from source — Powered By @R3V_X")
                edit("📤 Sending unsigned APK...")
                await upload_document(unsigned_apk, f"✅ <b>Unsigned APK</b> built from source — Powered By @R3V_X")
                edit("✅ APK build complete! Signed + Unsigned delivered. 🔥", keep_button=False)
            except Exception as e:
                log.warning("Telegram upload failed: %s", e)
                if not JOB_ID:
                    await send_error_log(work_dir, e, "Result upload failed")
        else:
            edit("✅ APK build complete! Signed + Unsigned delivered. 🔥", keep_button=False)
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
