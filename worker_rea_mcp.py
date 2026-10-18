#!/usr/bin/env python3
"""
Worker REA MCP:
Runs on GitHub Actions runner.
1. Spawns REA MCP Server + MCP SSE Bridge + Cloudflare Tunnel.
2. Performs automated initial reconnaissance & disassembly on target binary.
3. Automatically packages and uploads analysis artifacts (ZIP, reports, C code) to Telegram.
4. Streams live MCP tool executions, completions, and dynamic file creations one-by-one to Telegram.
5. Manages session lifecycle and graceful teardown.
"""

import os
import re
import sys
import time
import signal
import shutil
import logging
import tempfile
import threading
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("worker_rea_mcp")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FILE_URL = os.environ.get("PAYLOAD_FILE_URL", "")
TG_FILE_PATH = os.environ.get("PAYLOAD_TG_FILE_PATH", "")
CHAT_ID = os.environ.get("PAYLOAD_CHAT_ID", "")
MESSAGE_ID = os.environ.get("PAYLOAD_MESSAGE_ID", "")
FILENAME = os.environ.get("PAYLOAD_FILENAME", "")
JOB_ID = os.environ.get("PAYLOAD_JOB_ID", "")
IS_ADMIN = os.environ.get("PAYLOAD_IS_ADMIN", "False").lower() == "true"
SESSION_TIMEOUT_MINUTES = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "120"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STOP_FLAG = False


def signal_handler(sig, frame):
    global STOP_FLAG
    log.info("Received termination signal %s. Exiting session...", sig)
    STOP_FLAG = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def tg_send(method: str, **params):
    if not BOT_TOKEN:
        log.warning("BOT_TOKEN is empty; cannot send Telegram message")
        return None
    try:
        resp = httpx.post(f"{API}/{method}", json=params, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            log.warning("tg_send %s error response: %s", method, resp.text)
        return data
    except Exception as e:
        log.warning("tg_send %s failed: %s", method, e)
        return None


def tg_send_message(text: str, parse_mode: str = "HTML"):
    if not CHAT_ID:
        return None
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    return tg_send("sendMessage", **params)


def tg_send_document(file_path: Path, caption: str = "", parse_mode: str = "HTML"):
    if not BOT_TOKEN or not CHAT_ID or not file_path.exists():
        return False
    size = file_path.stat().st_size
    if size > 50 * 1024 * 1024:
        log.warning("File %s exceeds 50MB limit (%d bytes)", file_path.name, size)
        tg_send_message(
            f"⚠️ File <code>{file_path.name}</code> is {size / (1024 * 1024):.1f} MB "
            "(exceeds Telegram Bot API 50MB limit)."
        )
        return False

    url = f"{API}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            data = {"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": parse_mode}
            files = {"document": (file_path.name, f)}
            resp = httpx.post(url, data=data, files=files, timeout=120)
            res_json = resp.json()
            if res_json.get("ok"):
                log.info("Uploaded document %s to chat %s successfully", file_path.name, CHAT_ID)
                return True
            else:
                log.warning("tg_send_document error: %s", resp.text)
                return False
    except Exception as e:
        log.error("tg_send_document exception: %s", e)
        return False


def edit_status(text: str, reply_markup=None):
    if not CHAT_ID:
        return
    params = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup

    res = None
    if MESSAGE_ID:
        edit_params = dict(params, message_id=int(MESSAGE_ID))
        res = tg_send("editMessageText", **edit_params)

    if not res or not res.get("ok"):
        log.info("editMessageText unsuccessful; sending as new message to chat %s", CHAT_ID)
        tg_send("sendMessage", **params)


def download_target_file(workdir: Path) -> Path:
    if not FILENAME or (not FILE_URL and not TG_FILE_PATH):
        return None

    target_file = workdir / FILENAME
    log.info("Downloading target file to %s...", target_file)

    download_url = FILE_URL
    if not download_url and TG_FILE_PATH:
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{TG_FILE_PATH}"

    try:
        with httpx.stream("GET", download_url, follow_redirects=True, timeout=120) as r:
            if r.status_code != 200:
                log.warning("Failed to download file: status %s", r.status_code)
                return None
            with open(target_file, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    f.write(chunk)
        log.info("Target file saved successfully (%d bytes)", target_file.stat().st_size)
        return target_file
    except Exception as e:
        log.error("Download error: %s", e)
        return None


class WorkspaceFileWatcher(threading.Thread):
    """
    Background file watcher that scans the workspace for any newly created or modified
    files during the active MCP session, notifies Telegram, and automatically uploads them.
    """

    def __init__(self, watch_dir: Path, seen_files: set, exclude_patterns=None):
        super().__init__(daemon=True)
        self.watch_dir = watch_dir
        self.seen_files = seen_files
        self.ignored_substrings = [
            "cloudflared.log",
            "apktool",
            "decompiled_sources",
            "rea_analysis_artifacts",
            ".lock",
            ".tmp",
            ".swp",
            ".sock",
            ".git",
            "node_modules",
        ]
        self.running = True

    def run(self):
        log.info("Workspace file watcher active on %s", self.watch_dir)
        while self.running and not STOP_FLAG:
            try:
                for p in list(self.watch_dir.rglob("*")):
                    if not p.is_file():
                        continue
                    resolved = str(p.resolve())
                    if resolved in self.seen_files:
                        continue

                    # Check exclusion patterns
                    if any(sub in resolved for sub in self.ignored_substrings):
                        continue

                    # Verify file has stabilized (finished writing)
                    prev_size = -1
                    stable = False
                    for _ in range(4):
                        time.sleep(0.8)
                        if not p.exists():
                            break
                        cur_size = p.stat().st_size
                        if cur_size == prev_size and cur_size > 0:
                            stable = True
                            break
                        prev_size = cur_size

                    if not stable or not p.exists():
                        continue

                    self.seen_files.add(resolved)
                    try:
                        rel_name = p.relative_to(self.watch_dir)
                    except ValueError:
                        rel_name = p.name

                    size_kb = p.stat().st_size / 1024
                    log.info("Watcher detected new file: %s (%.1f KB)", rel_name, size_kb)

                    tg_send_message(
                        f"📄 <b>New Artifact Created by MCP:</b>\n"
                        f"📁 <code>{rel_name}</code> ({size_kb:.1f} KB)"
                    )

                    caption = f"📄 <b>MCP Artifact:</b> <code>{p.name}</code>"
                    tg_send_document(p, caption=caption)

            except Exception as e:
                log.warning("File watcher loop exception: %s", e)

            time.sleep(3)


def run_automated_recon(target_file: Path, workdir: Path, seen_files: set):
    """
    Executes automated reconnaissance and reverse engineering pipeline on target binary:
    1. Structure and manifest analysis.
    2. Native library disassembly and symbol extraction with Rizin.
    3. Source decompilation with JADX.
    4. Packages everything into a ZIP and delivers directly to Telegram.
    """
    log.info("Starting automated reconnaissance for %s...", target_file.name)
    recon_dir = workdir / "rea_analysis_artifacts"
    recon_dir.mkdir(parents=True, exist_ok=True)

    size_mb = target_file.stat().st_size / (1024 * 1024)

    # ---------------- Phase 1: Structure & Metadata ----------------
    tg_send_message(
        f"🔍 <b>[Recon 1/4] Analyzing Binary Structure:</b>\n"
        f"📁 Target: <code>{target_file.name}</code> ({size_mb:.2f} MB)"
    )

    is_apk = False
    try:
        if zipfile.is_zipfile(target_file):
            with zipfile.ZipFile(target_file, "r") as zf:
                namelist = zf.namelist()
                if "AndroidManifest.xml" in namelist or any(n.endswith(".dex") for n in namelist):
                    is_apk = True
    except Exception as e:
        log.warning("Zip inspection error: %s", e)

    manifest_summary = []
    if is_apk:
        # Run apktool to extract decoded AndroidManifest.xml and resources
        apktool_bin = shutil.which("apktool") or "apktool"
        apktool_out = Path(tempfile.mkdtemp(prefix="apktool_raw_"))
        log.info("Running apktool for manifest decode...")
        try:
            subprocess.run(
                [apktool_bin, "d", "-s", "-f", "-r", str(target_file), "-o", str(apktool_out)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
            )
        except Exception as e:
            log.warning("Apktool execution failed or timed out: %s", e)

        manifest_file = apktool_out / "AndroidManifest.xml"
        if manifest_file.exists():
            shutil.copy2(manifest_file, recon_dir / "AndroidManifest.xml")
            try:
                tree = ET.parse(manifest_file)
                root = tree.getroot()
                pkg = root.attrib.get("package", "Unknown")
                manifest_summary.append(f"Package: {pkg}")
                permissions = [
                    elem.attrib.get("{http://schemas.android.com/apk/res/android}name", elem.attrib.get("name", ""))
                    for elem in root.findall("uses-permission")
                ]
                activities = [
                    elem.attrib.get("{http://schemas.android.com/apk/res/android}name", elem.attrib.get("name", ""))
                    for elem in root.findall(".//activity")
                ]
                manifest_summary.append(f"Permissions Count: {len(permissions)}")
                manifest_summary.append(f"Activities Count: {len(activities)}")
                if activities:
                    manifest_summary.append(f"Main Activities: {', '.join(activities[:5])}")

                (recon_dir / "01_APK_Manifest_Details.txt").write_text(
                    f"=== AndroidManifest Breakdown ===\n\n"
                    f"Package Name: {pkg}\n\n"
                    f"Permissions ({len(permissions)}):\n" + "\n".join(f"- {p}" for p in permissions) + "\n\n"
                    f"Activities ({len(activities)}):\n" + "\n".join(f"- {a}" for a in activities) + "\n"
                )
            except Exception as e:
                log.warning("Manifest XML parse error: %s", e)

        try:
            shutil.rmtree(apktool_out, ignore_errors=True)
        except Exception:
            pass

        tg_send_message(
            f"✅ <b>[Recon 1/4 Complete]</b> File metadata & manifest mapped.\n"
            + ("\n".join(f"• {line}" for line in manifest_summary) if manifest_summary else "")
        )
    else:
        # Generic binary (ELF / PE / Mach-O)
        rizin_bin = shutil.which("rizin") or "/usr/local/bin/rizin"
        headers_file = recon_dir / "01_Binary_Headers.txt"
        try:
            res = subprocess.run(
                [rizin_bin, "-qc", "iI; iS; iE", str(target_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            headers_file.write_text(res.stdout or "No headers extracted.")
        except Exception as e:
            headers_file.write_text(f"Header extraction failed: {e}")

        tg_send_message("✅ <b>[Recon 1/4 Complete]</b> Binary headers & architecture mapped.")

    # ---------------- Phase 2: Native Symbols & Disassembly ----------------
    tg_send_message("⚡ <b>[Recon 2/4] Analyzing Native Code & Symbols with Rizin...</b>")
    rizin_bin = shutil.which("rizin") or "/usr/local/bin/rizin"

    if is_apk:
        native_dir = recon_dir / "native_libs"
        native_dir.mkdir(parents=True, exist_ok=True)
        so_files = []
        with zipfile.ZipFile(target_file, "r") as zf:
            for item in zf.namelist():
                if item.startswith("lib/") and item.endswith(".so"):
                    extracted_path = native_dir / Path(item).name
                    with open(extracted_path, "wb") as out_f:
                        out_f.write(zf.read(item))
                    so_files.append(extracted_path)

        if so_files:
            # Analyze largest native library
            primary_so = max(so_files, key=lambda f: f.stat().st_size)
            log.info("Analyzing primary native library: %s", primary_so.name)

            try:
                # Exported Symbols (JNI functions)
                res_sym = subprocess.run(
                    [rizin_bin, "-qc", "iE", str(primary_so)],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                (recon_dir / f"02_{primary_so.stem}_Exports.txt").write_text(res_sym.stdout or "")

                # Functions list
                res_fn = subprocess.run(
                    [rizin_bin, "-qc", "aa; afl", str(primary_so)],
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                (recon_dir / f"03_{primary_so.stem}_Functions.txt").write_text(res_fn.stdout or "")

                # Disassemble JNI functions
                res_jni = subprocess.run(
                    [rizin_bin, "-qc", "s sym.Java*; pdf", str(primary_so)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res_jni.stdout and len(res_jni.stdout.strip()) > 20:
                    (recon_dir / f"04_{primary_so.stem}_JNI_Disassembly.c").write_text(res_jni.stdout)

                # Strings
                res_str = subprocess.run(
                    [rizin_bin, "-qc", "izz", str(primary_so)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                (recon_dir / f"05_{primary_so.stem}_Strings.txt").write_text(res_str.stdout[:500000] if res_str.stdout else "")

                tg_send_message(
                    f"✅ <b>[Recon 2/4 Complete]</b> Native symbols extracted from <code>{primary_so.name}</code>.\n"
                    f"Exported functions and JNI bindings mapped."
                )
            except Exception as e:
                log.warning("Rizin native analysis error: %s", e)
                tg_send_message(f"⚠️ <b>[Recon 2/4]</b> Native analysis partial: {e}")
        else:
            tg_send_message("ℹ️ <b>[Recon 2/4]</b> No native <code>.so</code> libraries detected in APK.")
    else:
        try:
            res_fn = subprocess.run(
                [rizin_bin, "-qc", "aaa; afl", str(target_file)],
                capture_output=True,
                text=True,
                timeout=45,
            )
            (recon_dir / "02_Functions_List.txt").write_text(res_fn.stdout or "")

            res_sym = subprocess.run(
                [rizin_bin, "-qc", "iE; ii", str(target_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            (recon_dir / "03_Symbols_Imports.txt").write_text(res_sym.stdout or "")

            res_str = subprocess.run(
                [rizin_bin, "-qc", "izz", str(target_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            (recon_dir / "04_Strings.txt").write_text(res_str.stdout[:500000] if res_str.stdout else "")

            tg_send_message("✅ <b>[Recon 2/4 Complete]</b> Functions, symbols, and strings extracted.")
        except Exception as e:
            log.warning("Rizin analysis error: %s", e)

    # ---------------- Phase 3: Source Decompilation (JADX) ----------------
    jadx_bin = shutil.which("jadx") or "/usr/local/bin/jadx" or "/opt/jadx/bin/jadx"
    if is_apk and (shutil.which("jadx") or Path("/usr/local/bin/jadx").exists() or Path("/opt/jadx/bin/jadx").exists()):
        tg_send_message("☕ <b>[Recon 3/4] Decompiling Core Source Code with JADX...</b>")
        jadx_exec = shutil.which("jadx") or "/usr/local/bin/jadx"
        if not Path(jadx_exec).exists() and Path("/opt/jadx/bin/jadx").exists():
            jadx_exec = "/opt/jadx/bin/jadx"

        decompiled_dir = recon_dir / "decompiled_sources"
        try:
            res = subprocess.run(
                [jadx_exec, "-d", str(decompiled_dir), "--no-res", "-j", "4", str(target_file)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
            java_files = list(decompiled_dir.rglob("*.java"))
            (recon_dir / "06_Decompiled_Classes_Index.txt").write_text(
                f"Total Decompiled Java Classes: {len(java_files)}\n\n"
                + "\n".join(str(f.relative_to(decompiled_dir)) for f in java_files[:200])
            )
            tg_send_message(
                f"✅ <b>[Recon 3/4 Complete]</b> Java source decompilation finished.\n"
                f"Generated <b>{len(java_files)}</b> Java source files."
            )
        except Exception as e:
            log.warning("JADX decompilation failed or timed out: %s", e)
            tg_send_message(f"⚠️ <b>[Recon 3/4]</b> JADX decompilation timed out or skipped.")

    # ---------------- Phase 4: Package & Upload to Telegram ----------------
    tg_send_message("📦 <b>[Recon 4/4] Packaging Analysis Artifacts & Uploading...</b>")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(FILENAME).stem)[:40] or "target"
    zip_path = workdir / f"REA_Analysis_{safe_stem}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(recon_dir):
            for f in files:
                full_path = os.path.join(root, f)
                arcname = os.path.relpath(full_path, recon_dir)
                zf.write(full_path, arcname)

    # Register recon files in seen_files to prevent watcher duplicate uploads
    for p in recon_dir.rglob("*"):
        if p.is_file():
            seen_files.add(str(p.resolve()))
    seen_files.add(str(zip_path.resolve()))

    upload_ok = tg_send_document(
        zip_path,
        caption=(
            f"📊 <b>REA Automated Analysis Report</b>\n"
            f"📁 Target: <code>{target_file.name}</code>\n"
            f"📦 Includes Manifest, Native Symbols, Disassembly & Decompiled Sources."
        ),
    )

    if upload_ok:
        tg_send_message(
            "🎉 <b>Initial Analysis Reports Delivered!</b>\n"
            "All core analysis files have been sent above.\n"
            "Live MCP SSE Server is now standing by for interactive queries."
        )
    else:
        log.warning("Initial analysis ZIP delivery failed; sending individual text reports...")
        # Send primary text summaries individually
        for report in sorted(recon_dir.glob("*.txt")):
            tg_send_document(report, caption=f"📄 {report.name}")


def main():
    log.info("Starting REA MCP Worker (Session: %s)...", JOB_ID)

    edit_status(
        "🚀 <b>Starting REA MCP Environment...</b>\n"
        "⚡ Setting up Cloudflare Tunnel & AI Reverse Engineering tools...",
    )

    workdir = Path(tempfile.mkdtemp(prefix="rea_mcp_"))
    seen_files = set()

    target_file = download_target_file(workdir)
    if target_file and target_file.exists():
        seen_files.add(str(target_file.resolve()))

    # Prepare environment for REA
    rea_env = os.environ.copy()
    rea_env["PORT"] = "8080"
    rea_env["HOST"] = "127.0.0.1"
    rea_env["TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    rea_env["PAYLOAD_CHAT_ID"] = CHAT_ID
    rea_env["PAYLOAD_MESSAGE_ID"] = MESSAGE_ID
    rea_env["PAYLOAD_FILENAME"] = FILENAME or ""
    rea_env["REA_WORKSPACE"] = str(workdir)

    if target_file and target_file.exists():
        rea_env["REA_INITIAL_TARGET"] = str(target_file)

    # Path to mcp_sse_bridge.mjs
    bridge_script = Path(__file__).resolve().parent / "mcp_sse_bridge.mjs"
    if not bridge_script.exists():
        log.error("Bridge script not found: %s", bridge_script)
        edit_status("❌ <b>Error:</b> <code>mcp_sse_bridge.mjs</code> not found on runner.")
        return 1

    # Start MCP SSE Bridge
    log.info("Launching MCP SSE Bridge on port 8080...")
    bridge_proc = subprocess.Popen(
        ["node", str(bridge_script)],
        env=rea_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    time.sleep(2)

    # Start Cloudflare Tunnel
    log.info("Launching Cloudflare Tunnel...")
    cloudflared_bin = shutil.which("cloudflared") or "cloudflared"

    cf_proc = subprocess.Popen(
        [cloudflared_bin, "tunnel", "--url", "http://127.0.0.1:8080"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    # Extract Public Tunnel URL
    tunnel_url = None
    start_wait = time.time()
    url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

    while time.time() - start_wait < 30:
        line = cf_proc.stdout.readline()
        if not line:
            if cf_proc.poll() is not None:
                break
            time.sleep(0.5)
            continue
        print(f"[cloudflared] {line.strip()}", flush=True)
        match = url_pattern.search(line)
        if match:
            tunnel_url = match.group(0)
            break

    if not tunnel_url:
        log.error("Failed to obtain Cloudflare Tunnel URL within 30 seconds.")
        edit_status(
            "❌ <b>Cloudflare Tunnel Error</b>\n"
            "Could not generate public URL. Check runner logs for details."
        )
        if cf_proc.poll() is None:
            cf_proc.terminate()
        if bridge_proc.poll() is None:
            bridge_proc.terminate()
        return 1

    log.info("Cloudflare Tunnel active at: %s", tunnel_url)
    sse_endpoint = f"{tunnel_url}/sse"

    file_info = f"<code>{FILENAME}</code>" if FILENAME else "<i>Standalone Mode (No file pre-loaded)</i>"

    status_text = (
        "🚀 <b>REA MCP Server is LIVE!</b>\n\n"
        f"🔗 <b>MCP SSE URL:</b>\n<code>{sse_endpoint}</code>\n\n"
        f"📁 <b>Target Binary:</b> {file_info}\n"
        f"⏱️ <b>Session Limit:</b> {SESSION_TIMEOUT_MINUTES} minutes\n\n"
        "🛠️ <b>Available Engines & Tools:</b>\n"
        "• ⚙️ <b>Ghidra</b> — Decompilation & symbols\n"
        "• ⚡ <b>Rizin / Cutter</b> — Ultra-fast binary analysis\n"
        "• 📱 <b>Apktool & JADX</b> — Android APK / DEX reversing\n"
        "• 🪝 <b>Frida & GDB</b> — Dynamic memory instrumentation\n"
        "• 🧬 <b>.NET / WASM / EVM</b> — Smart contracts & managed code\n\n"
        "💡 <i>Copy the URL above and connect Claude Desktop, Cursor, or your AI Agent to begin reversing!</i>\n"
        "📡 <i>Live updates and generated files will stream directly below.</i>"
    )

    stop_markup = {
        "inline_keyboard": [
            [{"text": "🛑 Stop MCP Session", "callback_data": f"stop_{MESSAGE_ID}"}]
        ]
    }

    edit_status(status_text, reply_markup=stop_markup)

    # Start Background Workspace File Watcher
    watcher = WorkspaceFileWatcher(workdir, seen_files)
    watcher.start()

    # Execute Automated Initial Reconnaissance if target file is present
    if target_file and target_file.exists():
        try:
            run_automated_recon(target_file, workdir, seen_files)
        except Exception as e:
            log.error("Automated recon error: %s", e)
            tg_send_message(f"⚠️ Initial recon exception: {e}")

    # Monitor session loop
    session_start = time.time()
    max_duration = SESSION_TIMEOUT_MINUTES * 60

    try:
        while not STOP_FLAG:
            elapsed = time.time() - session_start
            if elapsed >= max_duration:
                log.info("Session time limit reached (%d min). Stopping.", SESSION_TIMEOUT_MINUTES)
                break

            # Check if processes are alive
            if bridge_proc.poll() is not None:
                log.warning("MCP SSE Bridge process exited unexpectedly.")
                break
            if cf_proc.poll() is not None:
                log.warning("Cloudflare tunnel exited unexpectedly.")
                break

            time.sleep(10)
    finally:
        log.info("Cleaning up MCP worker processes...")
        watcher.running = False

        if cf_proc.poll() is None:
            cf_proc.terminate()
            try:
                cf_proc.wait(timeout=5)
            except Exception:
                cf_proc.kill()

        if bridge_proc.poll() is None:
            bridge_proc.terminate()
            try:
                bridge_proc.wait(timeout=5)
            except Exception:
                bridge_proc.kill()

        edit_status(
            "🏁 <b>REA MCP Session Ended.</b>\n"
            "Server and Cloudflare tunnel have been safely closed.\n"
            "To start a new session, send a file or use <code>/mcp</code>.",
            reply_markup=None,
        )

        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
