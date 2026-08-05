import re

files = ["worker.py", "worker_apktool.py", "worker_apktool_build.py", "worker_jadx.py"]

for fname in files:
    with open(fname, "r") as f:
        content = f.read()

    # The buggy block is:
    #             if not mtproto_success and TG_FILE_PATH:
    #                 ...
    #             else:
    #                 filename_dl = await asyncio.wait_for(download_url(FILE_URL, dest, on_dl), timeout=1800)

    # Note that in worker_apktool_build.py, it's `download_url(FILE_URL, dest_path, on_dl)` or similar? 
    # Let's check exactly what the `else:` block contains in worker_jadx.py.

    # We want to change the `else:` block into `elif not mtproto_success and FILE_URL:`
    # And maybe add a final `elif not mtproto_success: raise ValueError(...)`
    
    # We will just replace:
    #             else:
    #                 filename_dl = await asyncio.wait_for(
    
    # Actually, we can use regex to replace `            else:\n                filename_dl = ` 
    # with `            elif not mtproto_success and FILE_URL:\n                filename_dl = `
    
    # Let's replace precisely:
    pattern = r'            else:\n                filename_dl = await asyncio\.wait_for\(download_url\(FILE_URL, (.*?), on_dl\), timeout=1800\)'
    
    def replacer(match):
        dest_var = match.group(1)
        return f'''            elif not mtproto_success and FILE_URL:
                filename_dl = await asyncio.wait_for(download_url(FILE_URL, {dest_var}, on_dl), timeout=1800)
            elif not mtproto_success:
                raise ValueError("No download method available or all failed.")'''
                
    content, count = re.subn(pattern, replacer, content)
    if count == 0:
        print(f"Failed to patch {fname}")
    else:
        with open(fname, "w") as f:
            f.write(content)
        print(f"Patched {fname}")

