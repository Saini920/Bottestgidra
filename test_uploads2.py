import urllib.request
import urllib.parse
import os

with open("test.txt", "w") as f:
    f.write("Hello World")

def upload_transfer_sh():
    with open("test.txt", "rb") as f:
        req = urllib.request.Request("https://transfer.sh/test.txt", data=f.read(), method="PUT")
        try:
            with urllib.request.urlopen(req) as resp:
                print("transfer.sh:", resp.read().decode())
        except Exception as e:
            print("transfer.sh:", e)

def upload_uguu_se():
    # Uguu.se expects multipart/form-data, skip complex implementation and just try a simple curl in bash
    pass

upload_transfer_sh()
