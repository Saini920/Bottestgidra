import re

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

for fname in files:
    with open(fname, "r") as f:
        content = f.read()

    # The download block we want to replace starts with:
    #             file_id = os.environ.get("PAYLOAD_FILE_ID", "")
    #             if file_id and os.environ.get("API_ID", "").strip():
    
    old_dl = """            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            if file_id and os.environ.get("API_ID", "").strip():
                filename = FILENAME or "download.bin"
                await on_dl(0.0)
                import sys
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Download failed with code {proc.returncode}")
            elif TG_FILE_PATH:"""

    # We modify it to fallback if MTProto fails
    new_dl = """            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            api_id = os.environ.get("API_ID", "").strip()
            api_hash = os.environ.get("API_HASH", "").strip()
            mtproto_success = False
            
            if file_id and api_id and api_hash:
                filename = FILENAME or "download.bin"
                await on_dl(0.0)
                import sys
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    mtproto_success = True
                else:
                    print(f"MTProto failed with code {proc.returncode}, falling back to HTTP...")

            if not mtproto_success and TG_FILE_PATH:"""

    # Note: `import sys` might not be there in all workers. Let's make it robust using regex.
    pattern = r'            file_id = os\.environ\.get\("PAYLOAD_FILE_ID", ""\).*?elif TG_FILE_PATH:'
    
    new_dl_regex = r"""            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            api_id = os.environ.get("API_ID", "").strip()
            api_hash = os.environ.get("API_HASH", "").strip()
            mtproto_success = False
            
            if file_id and api_id and api_hash:
                filename = FILENAME or "download.bin"
                await on_dl(0.0)
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    mtproto_success = True
                else:
                    print(f"MTProto Download Failed (code {proc.returncode}), trying fallback...")

            if not mtproto_success and TG_FILE_PATH:"""
            
    content = re.sub(pattern, new_dl_regex, content, flags=re.DOTALL)
    
    with open(fname, "w") as f:
        f.write(content)

print("DL Patched")
