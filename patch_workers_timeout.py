import re

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

old_block = """                dl_logs = []
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line: dl_logs.append(line)
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = float(line.split(":")[1])
                            await on_dl(pct)
                        except ValueError:
                            pass
                await proc.wait()"""

new_block = """                dl_logs = []
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
                    print("MTProto download timed out after 30 minutes")"""

for fname in files:
    with open(fname, "r") as f:
        content = f.read()
    
    content = content.replace(old_block, new_block)
    
    with open(fname, "w") as f:
        f.write(content)

print("Workers patched")
