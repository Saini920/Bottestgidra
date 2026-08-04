import os, sys, asyncio
from pyrogram import Client

async def main():
    api_id = int(os.environ.get("API_ID", 0))
    api_hash = os.environ.get("API_HASH", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    file_id = os.environ.get("PAYLOAD_FILE_ID", "")
    dest_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/input_file"
    
    if not api_id or not api_hash or not bot_token or not file_id:
        print("Missing credentials or file_id for MTProto download.")
        sys.exit(1)
        
    print(f"Downloading file_id {file_id} via MTProto (Pyrogram)...")
    app = Client("worker_session", api_id=api_id, api_hash=api_hash, bot_token=bot_token, in_memory=True)
    async with app:
        await app.download_media(file_id, file_name=dest_path)
    print("Download complete.")

if __name__ == "__main__":
    asyncio.run(main())
