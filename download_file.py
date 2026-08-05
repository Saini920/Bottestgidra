import os, sys, asyncio
from pyrogram import Client

async def robust_download(app, media, dest_path):
    total = getattr(media, "file_size", 0)
    if total == 0 and hasattr(media, "document"):
        total = media.document.file_size
        
    downloaded = 0
    with open(dest_path, "wb") as f:
        stream = app.stream_media(media)
        while True:
            try:
                # 45 seconds timeout per chunk to prevent infinite network hangs
                chunk = await asyncio.wait_for(stream.__anext__(), timeout=45.0)
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100.0 / total
                    print(f"PROGRESS:{pct:.2f}", flush=True)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                print("ERROR: Chunk download timed out after 45 seconds!", flush=True)
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
        
    print(f"Downloading file_id {file_id} via MTProto (Pyrogram stream)...", flush=True)
    app = Client("worker_session", api_id=api_id, api_hash=api_hash, bot_token=bot_token, in_memory=True)
    
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
    asyncio.run(main())
