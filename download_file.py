import os, sys, asyncio
from pyrogram import Client

async def progress(current, total):
    if total > 0:
        pct = current * 100.0 / total
        print(f"PROGRESS:{pct:.2f}", flush=True)

async def main():
    api_id = int(os.environ.get("API_ID", 0))
    api_hash = os.environ.get("API_HASH", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    file_id = os.environ.get("PAYLOAD_FILE_ID", "")
    chat_id = int(os.environ.get("PAYLOAD_CHAT_ID", 0))
    orig_msg_id = int(os.environ.get("PAYLOAD_ORIGINAL_MESSAGE_ID", 0))
    dest_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/input_file"
    
    if not api_id or not api_hash or not bot_token or not file_id:
        print("Missing credentials or file_id for MTProto download.")
        sys.exit(1)
        
    print(f"Downloading file_id {file_id} via MTProto (Pyrogram)...", flush=True)
    app = Client("worker_session", api_id=api_id, api_hash=api_hash, bot_token=bot_token, in_memory=True)
    async with app:
        if chat_id and orig_msg_id:
            msg = await app.get_messages(chat_id, orig_msg_id)
            if msg and msg.document:
                await app.download_media(msg.document, file_name=dest_path, progress=progress)
            else:
                print("Failed to fetch message or no document found. Trying fallback...", flush=True)
                await app.download_media(file_id, file_name=dest_path, progress=progress)
        else:
            await app.download_media(file_id, file_name=dest_path, progress=progress)
    print("Download complete.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
