import re

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

for fname in files:
    with open(fname, "r") as f:
        content = f.read()

    # We want to replace the `async for raw in proc.stdout:` block for BOTH download and upload.
    # To do this safely, we can replace:
    
    # 1. Download block
    old_dl = """                async for raw in proc.stdout:
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
                    print(f"MTProto Download Failed (code {proc.returncode}), trying fallback...")"""

    new_dl = """                dl_logs = []
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line: dl_logs.append(line)
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
                    err_msg = "\\n".join(dl_logs[-10:])
                    print(f"MTProto Download Failed (code {proc.returncode}). Logs:\\n{err_msg}\\nTrying fallback...")
                    edit(f"⚠️ MTProto Download Failed! Check GitHub Actions logs.\\n<code>{err_msg[-200:]}</code>", keep_button=False)"""
                    
    content = content.replace(old_dl, new_dl)

    # 2. Upload block
    old_ul = """                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = int(float(line.split(":")[1]))
                            await on_up(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    raise ValueError(f"MTProto Upload failed with code {proc.returncode}")"""

    new_ul = """                ul_logs = []
                async for raw in proc.stdout:
                    line = raw.decode(errors="replace").strip()
                    if line: ul_logs.append(line)
                    if line.startswith("PROGRESS:"):
                        try:
                            pct = int(float(line.split(":")[1]))
                            await on_up(pct)
                        except ValueError:
                            pass
                await proc.wait()
                if proc.returncode != 0:
                    err_msg = "\\n".join(ul_logs[-10:])
                    raise ValueError(f"MTProto Upload failed with code {proc.returncode}:\\n{err_msg}")"""

    content = content.replace(old_ul, new_ul)
    
    with open(fname, "w") as f:
        f.write(content)
        
print("Workers patched for MTProto debugging")
