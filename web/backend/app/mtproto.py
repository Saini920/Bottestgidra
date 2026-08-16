"""MTProto layer: per-user Pyrogram clients + the phone → OTP → 2FA password login flow."""
import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

from . import config, db, security

log = logging.getLogger("mtproto")

# Logged-in clients per user (lazy-loaded from stored session strings)
USER_CLIENTS: Dict[int, Client] = {}
_client_lock = asyncio.Lock()


@dataclass
class PendingLogin:
    phone: str
    client: Client
    phone_code_hash: str = ""


PENDING: Dict[str, PendingLogin] = {}


def _creds():
    """Current API credentials (from env or the web Settings page)."""
    api_id = config.get_api_id()
    api_hash = config.get_api_hash()
    if not api_id or not api_hash:
        raise RuntimeError("API_ID / API_HASH not configured yet — open the Setup page")
    return api_id, api_hash


async def send_code(phone: str) -> dict:
    """Step 1 — request an OTP code. Returns error message or None."""
    try:
        api_id, api_hash = _creds()
    except RuntimeError as e:
        return {"error": str(e)}
    client = Client(f"login_{uuid.uuid4().hex[:8]}", api_id=api_id,
                    api_hash=api_hash, in_memory=True)
    try:
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        return {"error": "Invalid phone number. Use international format, e.g. +91XXXXXXXXXX."}
    except FloodWait as e:
        return {"error": f"Too many attempts. Please wait {int(e.value)} seconds and retry."}
    except Exception as e:
        log.exception("send_code failed")
        return {"error": f"Failed to send code: {type(e).__name__}"}

    PENDING[phone] = PendingLogin(phone=phone, client=client, phone_code_hash=sent.phone_code_hash)
    return {"ok": True, "hint": "Enter the 5-digit code you received on Telegram."}


async def verify_code(phone: str, code: str) -> dict:
    """Step 2 — verify the OTP. Returns password_required=True if 2FA is enabled."""
    pending = PENDING.get(phone)
    if not pending:
        return {"error": "Session expired. Please start again with your phone number."}
    client = pending.client
    try:
        await client.sign_in(phone, code, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeeded:
        return {"ok": True, "password_required": True}
    except PhoneCodeInvalid:
        return {"error": "Incorrect code. Please check and try again."}
    except PhoneCodeExpired:
        PENDING.pop(phone, None)
        return {"error": "Code expired. Please request a new code."}
    except FloodWait as e:
        return {"error": f"Too many attempts. Please wait {int(e.value)} seconds."}
    except Exception as e:
        log.exception("verify_code failed")
        return {"error": f"Login failed: {type(e).__name__}"}
    return await _finalize(pending, phone)


async def check_password(phone: str, password: str) -> dict:
    """Step 3 — only shown when the account has 2FA enabled."""
    pending = PENDING.get(phone)
    if not pending:
        return {"error": "Session expired. Please start again with your phone number."}
    client = pending.client
    try:
        await client.check_password(password)
    except PasswordHashInvalid:
        return {"error": "Wrong 2FA password. Please try again."}
    except FloodWait as e:
        return {"error": f"Too many attempts. Please wait {int(e.value)} seconds."}
    except Exception as e:
        log.exception("check_password failed")
        return {"error": f"Login failed: {type(e).__name__}"}
    return await _finalize(pending, phone)


async def _finalize(pending: PendingLogin, phone: str) -> dict:
    """Common post-login step: export session, persist user, cache client."""
    client = pending.client
    PENDING.pop(phone, None)
    try:
        me = await client.get_me()
        session_str = await client.export_session_string()
    except Exception as e:
        log.exception("finalize failed")
        return {"error": f"Login failed: {type(e).__name__}"}

    user_id = me.id
    # First user ever to log in becomes the admin (bootstrap — then manage
    # admins via the Settings page / ADMIN_IDS).
    is_admin = bool(user_id in config.get_admin_ids()) or (db.count_admins() == 0 and db.count_users() == 0)
    db.upsert_user(
        user_id=user_id,
        name=me.first_name or "",
        username=me.username or "",
        phone=phone,
        session_enc=security.encrypt_session(session_str),
    )
    if is_admin:
        db.set_admin(user_id, 1)
    async with _client_lock:
        USER_CLIENTS[user_id] = client
    token = security.make_token(user_id)
    return {
        "ok": True,
        "token": token,
        "user": {
            "id": user_id,
            "name": me.first_name or "",
            "username": me.username or "",
            "is_admin": is_admin,
        },
    }


async def get_client(user_id: int) -> Optional[Client]:
    """Return a usable Pyrogram client for the user, creating it lazily."""
    client = USER_CLIENTS.get(user_id)
    if client is not None:
        return client
    async with _client_lock:
        client = USER_CLIENTS.get(user_id)
        if client is not None:
            return client
        user = db.get_user(user_id)
        if not user or not user.get("session_enc"):
            return None
        session_str = security.decrypt_session(user["session_enc"])
        if not session_str:
            return None
        try:
            client = Client(
                f"user_{user_id}",
                api_id=config.get_api_id(),
                api_hash=config.get_api_hash(),
                session_string=session_str,
                in_memory=True,
            )
            await client.start()
            USER_CLIENTS[user_id] = client
            return client
        except Exception as e:
            log.warning("client restart failed for user %s: %s", user_id, e)
            return None


async def logout(user_id: int):
    client = USER_CLIENTS.pop(user_id, None)
    if client is not None:
        try:
            await client.log_out()
        except Exception:
            pass
    db.clear_session(user_id)
