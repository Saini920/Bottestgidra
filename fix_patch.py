import glob
files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]
for f in files:
    with open(f, "r") as fh:
        content = fh.read()
    
    # We will find the entire `try:` block starting from `api_id_check = os.environ.get("API_ID", "").strip()`
    # until `edit("✅ Decompilation complete! ZIP file delivered via MTProto. 🔥", keep_button=False)` or similar.
    # Actually, it's easier to just do a string replacement on the broken parts.
    
    bad_proc = """            api_id_check = os.environ.get("API_ID", "").strip()
            if api_id_check:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", """
    
    if "final_apk" in content: # worker_apktool_build.py
        target_var = "final_apk"
    else:
        target_var = "zip_path"
        
    good_block = f"""            api_id_check = os.environ.get("API_ID", "").strip()
            if api_id_check:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "upload_file.py", str({target_var}), caption,
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
                with open(str({target_var}), "rb") as doc_f:
                    url = f"https://api.telegram.org/bot{{BOT_TOKEN}}/sendDocument"
                    data = {{"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}}
                    files = {{"document": doc_f}}
                    async with httpx.AsyncClient(timeout=300) as client:
                        resp = await client.post(url, data=data, files=files)
                        resp.raise_for_status()
            await on_up(100)"""

    # We need to find the old block in the file and replace it.
    import re
    # Match from api_id_check down to await on_up(100)
    pattern = r'api_id_check = os\.environ\.get\("API_ID", ""\)\.strip\(\)\n.*?await on_up\(100\)'
    content = re.sub(pattern, good_block, content, flags=re.DOTALL)
    
    # Let's also fix the success message string which might have been hardcoded.
    with open(f, "w") as fh:
        fh.write(content)
print("Fixed successfully")
