import httpx
import os

with open("test.txt", "w") as f:
    f.write("Hello World")

def test_upload(name, url, files=None, data=None):
    try:
        r = httpx.post(url, files=files, data=data, timeout=30)
        print(f"{name}: {r.status_code} - {r.text.strip()}")
    except Exception as e:
        print(f"{name}: Failed - {e}")

# file.io
test_upload("file.io", "https://file.io", files={'file': open("test.txt", "rb")})

# 0x0.st
test_upload("0x0.st", "https://0x0.st", files={'file': open("test.txt", "rb")})

# bashupload.com
test_upload("bashupload", "https://bashupload.com", files={'file': open("test.txt", "rb")})

