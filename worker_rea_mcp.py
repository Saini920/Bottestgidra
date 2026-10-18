#!/usr/bin/env python3
"""
Worker REA MCP:
Runs on GitHub Actions runner.
Spawns REA MCP Server + MCP SSE Bridge + Cloudflare Tunnel.
Sends the live MCP SSE URL to Telegram and manages the session lifecycle.
"""

import os
import re
import sys
import time
import signal
import shutil
import logging
import tempfile
import subprocess
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


def main():
    log.info("Starting REA MCP Worker (Session: %s)...", JOB_ID)

    edit_status(
        "🚀 <b>Starting REA MCP Environment...</b>\n"
        "⚡ Setting up Cloudflare Tunnel & AI Reverse Engineering tools...",
    )

    workdir = Path(tempfile.mkdtemp(prefix="rea_mcp_"))
    target_file = download_target_file(workdir)

    # Prepare environment for REA
    rea_env = os.environ.copy()
    rea_env["PORT"] = "8080"
    rea_env["HOST"] = "127.0.0.1"

    if target_file and target_file.exists():
        rea_env["REA_INITIAL_TARGET"] = str(target_file)
        rea_env["REA_WORKSPACE"] = str(workdir)

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
    cf_log_path = workdir / "cloudflared.log"

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
        "💡 <i>Copy the URL above and connect Claude Desktop, Cursor, or your AI Agent to begin reversing!</i>"
    )

    stop_markup = {
        "inline_keyboard": [
            [{"text": "🛑 Stop MCP Session", "callback_data": f"stop_{MESSAGE_ID}"}]
        ]
    }

    edit_status(status_text, reply_markup=stop_markup)

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
