import re
import glob

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

for fname in files:
    with open(fname, "r") as f:
        content = f.read()

    # The block we want to replace starts with:
    #             file_id = os.environ.get("PAYLOAD_FILE_ID", "")
    # and ends with:
    #             elif not mtproto_success:
    #                 raise ValueError("No MTProto, no TG_FILE_PATH, and no FILE_URL provided.")

    pattern = r'            file_id = os\.environ\.get\("PAYLOAD_FILE_ID", ""\).*?elif not mtproto_success:\s+raise ValueError\("No MTProto, no TG_FILE_PATH, and no FILE_URL provided\."\)'
    
    new_dl = """            file_id = os.environ.get("PAYLOAD_FILE_ID", "")
            api_id = os.environ.get("API_ID", "").strip()
            api_hash = os.environ.get("API_HASH", "").strip()
            http_success = False
            
            if TG_FILE_PATH:
                filename = FILENAME or "download.bin"
                tg_url = TG_FILE_PATH if TG_FILE_PATH.startswith("http") else f"{API}/file/{TG_FILE_PATH}"
                try:
                    async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(120, read=300)) as client:
                        async with client.stream("GET", tg_url) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length") or 0)
                            downloaded = 0
                            with open(dest, "wb") as fh:
                                async for chunk in resp.aiter_bytes(65536):
                                    fh.write(chunk)
                                    downloaded += len(chunk)
                                    if total:
                                        pct = min(100, int(downloaded * 100 / total))
                                        await on_dl(pct)
                    if dest.exists() and dest.stat().st_size > 0:
                        http_success = True
                except Exception as e:
                    print(f"HTTP TG Download failed: {e}, falling back to MTProto")
            elif FILE_URL:
                try:
                    filename = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)
                    if dest.exists() and dest.stat().st_size > 0:
                        http_success = True
                except Exception as e:
                    print(f"HTTP URL Download failed: {e}, falling back to MTProto")

            mtproto_success = False
            if not http_success and file_id and api_id and api_hash:
                filename = FILENAME or "download.bin"
                await on_dl(0.0)
                import sys
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "download_file.py", str(dest),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                dl_logs = []
                async def read_stream():
                    async for raw in proc.stdout:
                        line = raw.decode(errors="replace").strip()
                        if line: dl_logs.append(line)
                        if line.startswith("PROGRESS:"):
                            try:
                                pct = float(line.split(":")[1])
                                await on_dl(pct)
                            except ValueError:
                                pass
                try:
                    await asyncio.wait_for(read_stream(), timeout=1800)
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    try: proc.kill()
                    except: pass
                    print("MTProto download timed out after 30 minutes")
                if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                    mtproto_success = True
                else:
                    err_msg = "\\n".join(dl_logs[-10:])
                    print(f"MTProto Download Failed (code {proc.returncode}). Logs:\\n{err_msg}")
                    edit(f"⚠️ MTProto Download Failed! Check GitHub Actions logs.\\n<code>{err_msg[-200:]}</code>", keep_button=False)

            if not http_success and not mtproto_success:
                raise ValueError("No MTProto, no TG_FILE_PATH, and no FILE_URL succeeded.")"""
                
    content, count = re.subn(pattern, new_dl, content, flags=re.DOTALL)
    if count > 0:
        with open(fname, "w") as f:
            f.write(content)
        print(f"Patched {fname}")
    else:
        print(f"Pattern not found in {fname}")
