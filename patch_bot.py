import re

with open("bot.py", "r") as f:
    content = f.read()

# We want to change the return value of download_file_for_bot to raise an exception with the error message so the outer try-except catches it and displays it.
# Actually, the outer block is:
#                 ok = await download_file_for_bot(job, dest, update_inspect_progress)
#                 ...
#                 if ok and dest.exists() and dest.stat().st_size > 0:
#                     ...
#                 else:
#                     await status_wrap.edit_text("❌ Could not retrieve file for inspection. ...")

# Let's modify bot.py to raise ValueError in download_file_for_bot if MTProto fails.

old_mtproto = """            if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return True
            else:
                err_msg = "\\n".join(dl_logs[-10:])
                log.error(f"bot.py MTProto Download Failed (code {proc.returncode}). Logs:\\n{err_msg}")
        except Exception as e:
            log.warning("MTProto download failed in bot.py: %s", e)"""

new_mtproto = """            if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
                return True
            else:
                err_msg = "\\n".join(dl_logs[-10:])
                log.error(f"bot.py MTProto Download Failed (code {proc.returncode}). Logs:\\n{err_msg}")
                raise ValueError(f"MTProto Download Failed:\\n{err_msg}")
        except Exception as e:
            log.warning("MTProto download failed in bot.py: %s", e)
            raise"""

content = content.replace(old_mtproto, new_mtproto)

# And in the HTTP block:
old_http = """            if dest.exists() and dest.stat().st_size > 0:
                return True
        except Exception as e:
            log.warning("HTTP download failed in bot.py: %s", e)

    return False"""

new_http = """            if dest.exists() and dest.stat().st_size > 0:
                return True
        except Exception as e:
            log.warning("HTTP download failed in bot.py: %s", e)
            raise ValueError(f"HTTP download failed: {e}")

    raise ValueError("No valid URL or File ID found to download.")"""

content = content.replace(old_http, new_http)

with open("bot.py", "w") as f:
    f.write(content)

print("Patched bot.py")
