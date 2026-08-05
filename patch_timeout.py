import re

with open("bot.py", "r") as f:
    content = f.read()

old_block = """            dl_logs = []
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line: dl_logs.append(line)
                if line.startswith("PROGRESS:") and progress_cb:
                    try:
                        pct = float(line.split(":")[1])
                        await progress_cb(min(45.0, pct * 0.45), "📥 <b>Downloading file via MTProto...</b>")
                    except ValueError:
                        pass
            await proc.wait()"""

new_block = """            dl_logs = []
            import asyncio
            async def read_stream():
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line: dl_logs.append(line)
                    if line.startswith("PROGRESS:") and progress_cb:
                        try:
                            pct = float(line.split(":")[1])
                            await progress_cb(min(45.0, pct * 0.45), "📥 <b>Downloading file via MTProto...</b>")
                        except ValueError:
                            pass
            try:
                await asyncio.wait_for(read_stream(), timeout=1800)
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                try: proc.kill()
                except: pass
                raise ValueError("MTProto download timed out after 30 minutes")"""

content = content.replace(old_block, new_block)

with open("bot.py", "w") as f:
    f.write(content)
print("bot.py patched")
