import os, sys, asyncio, hashlib
from pyrogram import Client
from pyrogram.errors import FloodWait, Unauthorized

async def robust_download(app, media, dest_path):
    import time
    last_progress_time = time.time()
    
    async def progress(current, total):
        nonlocal last_progress_time
        last_progress_time = time.time()
        if total > 0:
            pct = current * 100.0 / total
            print(f"PROGRESS:{pct:.2f}", flush=True)

    async def watchdog():
        while True:
            await asyncio.sleep(5)
            if time.time() - last_progress_time > 45:
                print("ERROR: Download connection stalled for 45 seconds! Exiting...", flush=True)
                os._exit(1)

    watchdog_task = asyncio.create_task(watchdog())
    task = asyncio.create_task(app.download_media(media, file_name=dest_path, progress=progress))
    try:
        # Give it up to 60 minutes overall
        await asyncio.wait_for(task, timeout=3600)
    except asyncio.TimeoutError:
        print("ERROR: Download timed out after 60 minutes!", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Pyrogram download failed: {e}", flush=True)
        sys.exit(1)
    finally:
        watchdog_task.cancel()

async def main():
    raw_api = os.environ.get("API_ID", "").strip()
    api_id = int(raw_api) if raw_api.isdigit() else 0
    api_hash = os.environ.get("API_HASH", "").strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    file_id = os.environ.get("PAYLOAD_FILE_ID", "").strip()
    
    raw_chat = os.environ.get("PAYLOAD_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else 0
    
    raw_orig = os.environ.get("PAYLOAD_ORIGINAL_MESSAGE_ID", "").strip()
    orig_msg_id = int(raw_orig) if raw_orig.isdigit() else 0
    
    dest_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/input_file"
    
    if not api_id or not api_hash or not bot_token or not file_id:
        print("Missing credentials or file_id for MTProto download.")
        sys.exit(1)

    # Use a stable pool of session files so Telegram's 'auth.ImportBotAuthorization'
    # is NOT repeated on every download (repeated logins trigger FloodWait).
    # NOTE: python's built-in hash() is randomized per process (PYTHONHASHSEED),
    # so we must use hashlib for a stable session name across runs.
    pool_id = int(hashlib.md5(file_id.encode("utf-8")).hexdigest(), 16) % 5
    session_name = f"worker_session_pool_{pool_id}"
    app = Client(session_name, api_id=api_id, api_hash=api_hash, bot_token=bot_token, workdir="/tmp")

    last_error = None
    for attempt in range(3):
        try:
            async with app:
                if chat_id and orig_msg_id:
                    msg = await app.get_messages(chat_id, orig_msg_id)
                    if msg and msg.document:
                        await robust_download(app, msg, dest_path)
                    else:
                        print("Failed to fetch message or no document found. Trying fallback...", flush=True)
                        await robust_download(app, file_id, dest_path)
                else:
                    await robust_download(app, file_id, dest_path)
            print("Download complete.", flush=True)
            return
        except FloodWait as e:
            last_error = e
            wait = min(e.retry_after, 90)
            print(f"WARN: FloodWait {e.retry_after}s (auth), attempt {attempt+1}/3. Sleeping {wait}s...", flush=True)
            await asyncio.sleep(wait)
        except Unauthorized as e:
            last_error = e
            print(f"WARN: Unauthorized on auth, attempt {attempt+1}/3. Sleeping 30s...", flush=True)
            await asyncio.sleep(30)
        except Exception as e:
            last_error = e
            print(f"WARN: MTProto auth/download error, attempt {attempt+1}/3: {e}", flush=True)
            await asyncio.sleep(5)

    print(f"ERROR: MTProto auth/download failed after 3 attempts: {last_error}", flush=True)
    sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        # IMPORTANT: KEEP the .session files so the bot authorization is reused and
        # 'auth.ImportBotAuthorization' FloodWait is avoided. Only remove stale journal/lock files.
        try:
            for f in os.listdir("/tmp"):
                if f.startswith("worker_session_") and f.endswith(".session-journal"):
                    try:
                        os.remove(f"/tmp/{f}")
                    except Exception:
                        pass
        except Exception:
            pass
