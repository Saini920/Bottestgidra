import os, sys, asyncio
from pyrogram import Client

async def robust_download(app, media, dest_path):
    async def progress(current, total):
        if total > 0:
            pct = current * 100.0 / total
            print(f"PROGRESS:{pct:.2f}", flush=True)

    task = asyncio.create_task(app.download_media(media, file_name=dest_path, progress=progress))
    try:
        # Give it up to 60 minutes overall, or we can just await it
        await asyncio.wait_for(task, timeout=3600)
    except asyncio.TimeoutError:
        print("ERROR: Download timed out after 60 minutes!", flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Pyrogram download failed: {e}", flush=True)
        sys.exit(1)

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
        
    session_name = f"worker_session_{os.urandom(4).hex()}"
    app = Client(session_name, api_id=api_id, api_hash=api_hash, bot_token=bot_token, workdir="/tmp")
    
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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        for f in os.listdir("/tmp"):
            if f.startswith("worker_session_") and (f.endswith(".session") or f.endswith(".session-journal")):
                try:
                    os.remove(f"/tmp/{f}")
                except:
                    pass
