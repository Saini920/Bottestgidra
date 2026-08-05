import re

files = [
    ("worker.py", "zip_path", "Ghidra"),
    ("worker_apktool.py", "zip_path", "Apktool"),
    ("worker_apktool_build.py", "final_apk", "Compilation"),
    ("worker_jadx.py", "zip_path", "JADX"),
]

for fname, var_name, engine_name in files:
    with open(fname, "r") as f:
        content = f.read()

    # 1. Fix Download Block
    # Replace `if file_id:` with `if file_id and os.environ.get("API_ID", "").strip():`
    content = content.replace("if file_id:", 'if file_id and os.environ.get("API_ID", "").strip():')

    # 2. Fix Upload Block
    # We replace the whole try-except subprocess block with a properly indented one.
    old_up_block = f"""        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "upload_file.py", str({var_name}), caption,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("PROGRESS:"):
                    try:
                        pct = int(float(line.split(":")[1]))
                        await on_up(pct)
                    except ValueError:
                        pass
            await proc.wait()
            if proc.returncode != 0:
                raise ValueError(f"MTProto Upload failed with code {{proc.returncode}}")
            await on_up(100)"""

    new_up_block = f"""        try:
            if os.environ.get("API_ID", "").strip():
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", str({var_name}), caption,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = int(float(line.split(":")[1]))
                            await on_up(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Upload failed with code {{proc.returncode}}")
            else:
                with open({var_name}, "rb") as doc_f:
                    url = f"https://api.telegram.org/bot{{BOT_TOKEN}}/sendDocument"
                    data = {{"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}}
                    files = {{"document": doc_f}}
                    async with httpx.AsyncClient(timeout=300) as client:
                        resp = await client.post(url, data=data, files=files)
                        resp.raise_for_status()
            await on_up(100)"""

    if old_up_block not in content:
        print(f"FAILED to find upload block in {fname}")
        continue

    content = content.replace(old_up_block, new_up_block)
    
    with open(fname, "w") as f:
        f.write(content)
        
print("All files patched")
