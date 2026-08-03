import json
import httpx
import logging

log = logging.getLogger(__name__)

class GistDB:
    def __init__(self, token):
        self.token = token
        self.headers = {"Authorization": f"token {self.token}", "Accept": "application/vnd.github.v3+json"}
        self.gist_id = None
        self.data = {"approved": [], "banned": [], "subscriptions": {}, "names": {}}
        if self.token:
            self.gist_id = self._get_or_create_gist()
            self._load()

    def _get_or_create_gist(self):
        try:
            r = httpx.get("https://api.github.com/gists", headers=self.headers, timeout=10)
            if r.status_code == 200:
                for g in r.json():
                    if "ghidra_bot_db.json" in g.get("files", {}):
                        return g["id"]
            
            # Create if not found
            payload = {
                "description": "Ghidra Telegram Bot Database",
                "public": False,
                "files": {
                    "ghidra_bot_db.json": {
                        "content": json.dumps(self.data)
                    }
                }
            }
            r = httpx.post("https://api.github.com/gists", headers=self.headers, json=payload, timeout=10)
            if r.status_code == 201:
                return r.json()["id"]
        except Exception as e:
            log.error(f"GistDB initialization error: {e}")
        return None

    def _load(self):
        if not self.gist_id: return
        try:
            r = httpx.get(f"https://api.github.com/gists/{self.gist_id}", headers=self.headers, timeout=10)
            if r.status_code == 200:
                content = r.json()["files"]["ghidra_bot_db.json"]["content"]
                loaded = json.loads(content)
                self.data["approved"] = loaded.get("approved", [])
                self.data["banned"] = loaded.get("banned", [])
                self.data["subscriptions"] = loaded.get("subscriptions", {})
                self.data["names"] = loaded.get("names", {})
        except Exception as e:
            log.error(f"GistDB load error: {e}")

    def save(self):
        if not self.gist_id: return
        try:
            payload = {
                "files": {
                    "ghidra_bot_db.json": {
                        "content": json.dumps(self.data, indent=2)
                    }
                }
            }
            httpx.patch(f"https://api.github.com/gists/{self.gist_id}", headers=self.headers, json=payload, timeout=10)
        except Exception as e:
            log.error(f"GistDB save error: {e}")

    def add_approved(self, uid: str, name: str = ""):
        if uid not in self.data["approved"]:
            self.data["approved"].append(uid)
        if uid in self.data["banned"]:
            self.data["banned"].remove(uid)
        if name:
            self.data["names"][uid] = name
        self.save()

    def remove_approved(self, uid: str):
        if uid in self.data["approved"]:
            self.data["approved"].remove(uid)
        self.save()

    def ban(self, uid: str, name: str = ""):
        if uid not in self.data["banned"]:
            self.data["banned"].append(uid)
        if uid in self.data["approved"]:
            self.data["approved"].remove(uid)
        if name:
            self.data["names"][uid] = name
        self.save()

    def unban(self, uid: str):
        if uid in self.data["banned"]:
            self.data["banned"].remove(uid)
        self.save()

    def set_sub(self, uid: str, expires_at: str, daily_limit: int, name: str = ""):
        self.data["subscriptions"][uid] = {"expires_at": expires_at, "daily_limit": daily_limit}
        if name:
            self.data["names"][uid] = name
        self.save()

    def remove_sub(self, uid: str):
        if uid in self.data["subscriptions"]:
            del self.data["subscriptions"][uid]
        self.save()

    def get_name(self, uid: str):
        return self.data["names"].get(uid, "Unknown")
