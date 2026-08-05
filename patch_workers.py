import os, glob

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

for f in files:
    with open(f, "r") as fh:
        content = fh.read()
    
    # 1. Fix Download
    content = content.replace(
        'if file_id:',
        'if file_id and os.environ.get("API_ID", "").strip():'
    )
    
    # 2. Fix Upload
    old_up = """        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", """
                
    new_up = """        try:
            api_id_check = os.environ.get("API_ID", "").strip()
            if api_id_check:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", """
                    
    content = content.replace(old_up, new_up)
    
    old_up_end = """            await proc.wait()
            if proc.returncode != 0:
                raise ValueError(f"MTProto Upload failed with code {proc.returncode}")
            await on_up(100)"""
            
    new_up_end = """            await proc.wait()
            if proc.returncode != 0:
                raise ValueError(f"MTProto Upload failed with code {proc.returncode}")
            else:
                with open(str(zip_path) if 'zip_path' in locals() else str(final_apk), "rb") as doc_f:
                    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
                    data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
                    files = {"document": doc_f}
                    async with httpx.AsyncClient(timeout=300) as client:
                        resp = await client.post(url, data=data, files=files)
                        resp.raise_for_status()
            await on_up(100)"""
    content = content.replace(old_up_end, new_up_end)
    
    with open(f, "w") as fh:
        fh.write(content)
        
print("Patched successfully")
