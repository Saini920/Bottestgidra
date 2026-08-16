"""Setup + Settings — configure everything from the web, no .env needed.

- `/api/setup/status` + `/api/setup`  → first-run wizard (no admin exists yet)
- `/api/settings` GET/PUT              → admin-only settings manager

All values are stored encrypted at rest (see db.set_setting / security).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import config, db
from .auth import current_user, require_admin

router = APIRouter(prefix="/api", tags=["setup"])


class SetupIn(BaseModel):
    api_id: str = ""
    api_hash: str = ""
    github_token: str = ""
    github_repo: str = ""
    public_url: str = ""


class SettingsIn(BaseModel):
    key: str
    value: str = ""


def _mask(value: str) -> str:
    if not value:
        return ""
    return "••••" + value[-4:] if len(value) > 4 else "••••"


@router.get("/setup/status")
async def setup_status():
    """Public: does the app need first-run setup? Shown on the login page."""
    api_id = config.get_api_id()
    api_hash = config.get_api_hash()
    admin_exists = db.count_admins() > 0
    return {
        "setup_done": bool(api_id and api_hash),
        "admin_exists": admin_exists,
    }


@router.post("/setup")
async def setup(body: SetupIn):
    """First-run wizard — only allowed while no admin exists yet."""
    if db.count_admins() > 0:
        raise HTTPException(403, "Setup is locked — an admin already exists")
    api_id = body.api_id.strip()
    api_hash = body.api_hash.strip()
    if not api_id.isdigit() or not api_hash:
        raise HTTPException(400, "API_ID must be numeric and API_HASH is required")
    db.set_setting("API_ID", api_id)
    db.set_setting("API_HASH", api_hash)
    if body.github_token.strip():
        db.set_setting("GITHUB_TOKEN", body.github_token.strip())
    if body.github_repo.strip():
        db.set_setting("GITHUB_REPO", body.github_repo.strip())
    if body.public_url.strip():
        db.set_setting("PUBLIC_URL", body.public_url.strip())
    return {"ok": True, "message": "Settings saved. Now log in — the first user becomes admin."}


@router.get("/settings")
async def get_settings(user_id: int = Depends(require_admin)):
    """Admin: current settings (masked)."""
    current = db.all_settings()
    return {"settings": {
        key: {"set": bool(current.get(key)), "masked": _mask(current.get(key, "")),
              "hint": _hint(key)}
        for key in db.SETTING_KEYS
    }}


@router.put("/settings")
async def put_settings(body: SettingsIn, user_id: int = Depends(require_admin)):
    """Admin: set one setting. Empty value clears it."""
    if not db.set_setting(body.key.strip(), body.value.strip()):
        raise HTTPException(400, f"Unknown setting key: {body.key}")
    return {"ok": True}


def _hint(key: str) -> str:
    hints = {
        "API_ID": "Telegram API ID (my.telegram.org)",
        "API_HASH": "Telegram API Hash (my.telegram.org)",
        "GITHUB_TOKEN": "GitHub PAT with repo scope (Actions dispatch)",
        "GITHUB_REPO": "owner/repo containing the workflows",
        "PUBLIC_URL": "Public URL of this backend",
        "WEBHOOK_TOKEN": "Token workers send to /api/status",
        "TELEGRAM_BOT_TOKEN": "Optional — keep the bot running (hybrid mode)",
        "ADMIN_IDS": "Comma-separated Telegram user IDs with admin rights",
    }
    return hints.get(key, "")
