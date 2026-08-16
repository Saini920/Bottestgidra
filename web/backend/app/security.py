"""Security helpers.

No .env needed: the master key comes from the SESSION_SECRET env var if set,
otherwise a random `secret.key` file is created next to the backend on first
run. The same key encrypts stored Telegram sessions AND web-configured settings.
"""
import base64
import hashlib
import hmac
import os
import time
from pathlib import Path

from nacl.secret import SecretBox

KEY_FILE = Path(__file__).resolve().parent.parent / "secret.key"
TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days


def _master_key() -> bytes:
    env = os.environ.get("SESSION_SECRET", "").strip()
    if env:
        return hashlib.sha256(env.encode("utf-8")).digest()
    if KEY_FILE.exists():
        raw = KEY_FILE.read_bytes()
        if len(raw) == 32:
            return raw
    key = os.urandom(32)
    KEY_FILE.write_bytes(key)
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    return key


_secret = _master_key()
_box = SecretBox(_secret)


def encrypt(value: str) -> str:
    """Encrypt a string with a fresh random nonce (prepended to ciphertext)."""
    return base64.b64encode(_box.encrypt(value.encode("utf-8"))).decode("utf-8")


def decrypt(blob: str) -> str:
    try:
        return _box.decrypt(base64.b64decode(blob.encode("utf-8"))).decode("utf-8")
    except Exception:
        return ""


# session-string helpers (aliases for readability)
def encrypt_session(session_string: str) -> str:
    return encrypt(session_string)


def decrypt_session(blob: str) -> str:
    return decrypt(blob)


def _sign(user_id: int, exp: int) -> str:
    msg = f"{user_id}:{exp}".encode("utf-8")
    return hmac.new(_secret, msg, hashlib.sha256).hexdigest()


def make_token(user_id: int) -> str:
    exp = int(time.time()) + TOKEN_TTL_SECONDS
    return f"{user_id}.{exp}.{_sign(user_id, exp)}"


def verify_token(token: str):
    """Return user_id or None."""
    try:
        user_id_s, exp_s, sig = token.split(".")
        user_id, exp = int(user_id_s), int(exp_s)
    except Exception:
        return None
    if exp < time.time():
        return None
    if not hmac.compare_digest(sig, _sign(user_id, exp)):
        return None
    return user_id


def make_signed_url(job_id: int, user_id: int, ttl_hours: int = 1) -> str:
    """Short-lived signed URL for a backend-hosted job file."""
    exp = int(time.time()) + ttl_hours * 3600
    return f"{_base_url()}/api/transfer/{job_id}/{user_id}/{exp}/{_transfer_sig(job_id, user_id, exp)}"


def verify_signed_url(job_id: int, user_id: int, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(sig, _transfer_sig(job_id, user_id, exp))


def _transfer_sig(job_id: int, user_id: int, exp: int) -> str:
    msg = f"{job_id}:{user_id}:{exp}".encode("utf-8")
    return hmac.new(_secret, msg, hashlib.sha256).hexdigest()


def _base_url() -> str:
    from . import config
    return config.get_public_url()
