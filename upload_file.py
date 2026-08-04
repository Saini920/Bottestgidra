import os, sys, asyncio
from pyrogram import Client

async def progress(current, total):
    if total > 0:
        pct = current * 100.0 / total
        print(f"PROGRESS:{pct:.2f}", flush=True)

async def main():
    raw_api = os.environ.get("API_ID", "").strip()
    api_id = int(raw_api) if raw_api.isdigit() else 0
    api_hash = os.environ.get("API_HASH", "").strip()
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    
    raw_chat = os.environ.get("PAYLOAD_CHAT_ID", "").strip()
    chat_id = int(raw_chat) if raw_chat.lstrip("-").isdigit() else 0
    
    file_path = sys.argv[1] if len(sys.argv) > 1 else ""
    caption = sys.argv[2] if len(sys.argv) > 2 else ""
    
    if not api_id or not api_hash or not bot_token or not chat_id or not file_path:
        print("Missing credentials or file_path for MTProto upload.")
        sys.exit(1)
        
    print(f"Uploading file to chat_id {chat_id} via MTProto (Pyrogram)...", flush=True)
    app = Client("worker_session", api_id=api_id, api_hash=api_hash, bot_token=bot_token, in_memory=True)
    async with app:
        await app.send_document(chat_id=int(chat_id), document=file_path, caption=caption, progress=progress)
    print("Upload complete.", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
