import os, sys, asyncio, hashlib
from pyrogram import Client
from pyrogram.errors import FloodWait, Unauthorized

# Dedicated directory so the session files can be persisted (Railway /opt volume)
# or cached between GitHub Actions runs. Reusing the same session avoids
# repeated 'auth.ImportBotAuthorization' calls which trigger Telegram FloodWait.
SESSION_DIR = os.environ.get("TG_SESSION_DIR", "/opt/tg_sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


class DownloadStallError(Exception):
    pass


async def robust_download(app, media, dest_path):
    import time
    last_progress_time = time.time()
    last_print_time = 0.0

    async def progress_cb(current, total):
        nonlocal last_progress_time, last_print_time
        now = time.time()
        last_progress_time = now
        if total > 0:
            pct = current * 100.0 / total
            if now - last_print_time >= 3.0 or current >= total:
                last_print_time = now
                print(f"PROGRESS:{pct:.2f}", flush=True)

    async def watchdog():
        while True:
            await asyncio.sleep(5)
            if time.time() - last_progress_time > 120:
                print("ERROR: Download connection stalled for 120 seconds!", flush=True)
                return  # signals a stall

    watchdog_task = asyncio.create_task(watchdog())
    main_task = asyncio.create_task(app.download_media(media, file_name=dest_path, progress=progress_cb))
    try:
        done, _ = await asyncio.wait(
            {main_task, watchdog_task},
            timeout=3600,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if watchdog_task in done:
            main_task.cancel()
            raise DownloadStallError("Download connection stalled for 120 seconds")
        if main_task not in done:
            main_task.cancel()
            raise TimeoutError("Download timed out after 60 minutes")
        exc = main_task.exception()
        if exc:
            raise exc
    finally:
        watchdog_task.cancel()
        if not main_task.done():
            main_task.cancel()


async def main():
    raw_api = os.environ.get("API_ID", "").strip()
    api_id = int(raw_api) if raw_api.isdigit() else 0
    api_hash = os.environ.get("API_HASH", "").strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    file_id = os.environ.get("PAYLOAD_FILE_ID", "").strip()

    raw_chat = os.environ.get("PAYLOAD_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else 0

    raw_orig = os.environ.get("PAYLOAD_ORIGINAL_MESSAGE_ID", "").strip() or os.environ.get("PAYLOAD_MESSAGE_ID", "").strip()
    orig_msg_id = int(raw_orig) if raw_orig.isdigit() else 0

    dest_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/input_file"

    if not api_id or not api_hash or not bot_token or not file_id:
        print("Missing credentials or file_id for MTProto download.")
        sys.exit(1)

    # 1. First attempt: Direct Bot API HTTP getFile download (Fast, zero MTProto errors for <= 20MB files)
    try:
        import httpx
        api_url = f"https://api.telegram.org/bot{bot_token}/getFile?file_id={file_id}"
        with httpx.Client(timeout=30.0) as client:
            r = client.get(api_url)
            if r.status_code == 200:
                res = r.json()
                if res.get("ok"):
                    fp = res["result"].get("file_path", "")
                    if fp:
                        dl_url = f"https://api.telegram.org/file/bot{bot_token}/{fp}"
                        with client.stream("GET", dl_url) as stream_resp:
                            if stream_resp.status_code == 200:
                                with open(dest_path, "wb") as f_out:
                                    for chunk in stream_resp.iter_bytes(65536):
                                        f_out.write(chunk)
                                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                                    print(f"Direct Bot API HTTP download succeeded: {os.path.getsize(dest_path)} bytes.", flush=True)
                                    return
    except Exception as http_ex:
        print(f"Direct HTTP getFile download skipped/failed: {http_ex}, trying MTProto...", flush=True)

    # 2. MTProto download via Pyrogram for large files
    pool_id = int(hashlib.md5(file_id.encode("utf-8")).hexdigest(), 16) % 5
    session_name = f"worker_session_pool_{pool_id}"
    app = Client(session_name, api_id=api_id, api_hash=api_hash, bot_token=bot_token, workdir=SESSION_DIR)

    print(f"Downloading file_id {file_id} via MTProto (Pyrogram)...", flush=True)

    def remove_session_file():
        import glob
        for f in glob.glob(os.path.join(SESSION_DIR, session_name + ".*")):
            try:
                os.remove(f)
                print(f"Removed stale session: {f}", flush=True)
            except OSError:
                pass

    last_error = None
    for attempt in range(5):
        try:
            async with app:
                media_msg = None
                if chat_id:
                    if orig_msg_id:
                        ids_to_check = [orig_msg_id + offset for offset in [0, -1, 1, -2, 2, -3, 3, -4, 4, -5, 5] if orig_msg_id + offset > 0]
                        try:
                            msgs = await app.get_messages(chat_id, ids_to_check)
                            if not isinstance(msgs, list):
                                msgs = [msgs]
                            for m in msgs:
                                if m and (m.document or m.video or m.audio or m.photo):
                                    media_msg = m
                                    break
                                if m and m.reply_to_message and (m.reply_to_message.document or m.reply_to_message.video or m.reply_to_message.audio or m.reply_to_message.photo):
                                    media_msg = m.reply_to_message
                                    break
                        except Exception as ge:
                            print(f"get_messages check notice: {ge}", flush=True)

                if media_msg:
                    print(f"Found media message id {media_msg.id} in chat {chat_id}, downloading...", flush=True)
                    await robust_download(app, media_msg, dest_path)
                else:
                    print("Message document not found. Trying download by file_id...", flush=True)
                    await robust_download(app, file_id, dest_path)

            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                print(f"Download complete: {os.path.getsize(dest_path)} bytes.", flush=True)
                return
            else:
                print("Warning: downloaded file is 0 bytes, retrying...", flush=True)
        except FloodWait as e:
            last_error = e
            wait = min(int(e.value) + 5, 900)
            print(f"WARN: FloodWait {e.value}s, attempt {attempt+1}/5. Waiting {wait}s...", flush=True)
            await asyncio.sleep(wait)
        except Unauthorized as e:
            last_error = e
            print(f"WARN: Unauthorized on auth, attempt {attempt+1}/5. Resetting this session...", flush=True)
            remove_session_file()
            await asyncio.sleep(30)
        except Exception as e:
            last_error = e
            print(f"WARN: MTProto download error, attempt {attempt+1}/5: {e}", flush=True)
            await asyncio.sleep(5)

    print(f"ERROR: MTProto download failed after 5 attempts: {last_error}", flush=True)
    sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
