"""App configuration.

Priority for every secret/setting:
    1. environment variable (if set),
    2. value configured via the web Settings page (stored encrypted in the DB),
    3. a sensible default.

So a fresh deploy works with ZERO env vars: open the Setup page, enter
API_ID/API_HASH (and optionally GITHUB_TOKEN/GITHUB_REPO), log in — done.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv():
    """Tiny .env loader (optional convenience, never required)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _db_setting(key: str):
    """Read a setting from the DB (encrypted at rest). Returns None if unset."""
    try:
        from . import db
        return db.get_setting(key)
    except Exception:
        return None


def _get(key: str, default: str = "") -> str:
    """env first, then web-configured setting, then default."""
    env_val = os.environ.get(key, "").strip()
    if env_val:
        return env_val
    db_val = _db_setting(key)
    if db_val:
        return db_val
    return default


# ---------- runtime getters (re-evaluated per call, so Settings changes apply live) ----------

def get_api_id() -> int:
    v = _get("API_ID")
    return int(v) if v.isdigit() else 0


def get_api_hash() -> str:
    return _get("API_HASH")


def get_github_token() -> str:
    return _get("GITHUB_TOKEN")


def get_github_repo() -> str:
    return _get("GITHUB_REPO", "Saini920/Bottestgidra")


def get_public_url() -> str:
    return _get("PUBLIC_URL", "http://localhost:8000")


def get_webhook_token() -> str:
    return _get("WEBHOOK_TOKEN", "dev-webhook-token")


def get_bot_token() -> str:
    return _get("TELEGRAM_BOT_TOKEN")


def get_admin_ids() -> list:
    ids = [x for x in _get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]
    return [int(x) for x in ids]


# ---------- static ----------

PORT = int(_env("PORT", "8000"))
DB_PATH = _env("DB_PATH", str(BASE_DIR / "data.db"))
TEMP_DIR = BASE_DIR / "tmp"
TEMP_DIR.mkdir(exist_ok=True)

# Telegram cloud-storage limits (metadata only — files live in user's Saved Messages)
FREE_STORAGE_BYTES = 2 * 1024**3
PREMIUM_STORAGE_BYTES = 4 * 1024**3

# GitHub Actions dispatch event names (same as the bot project's workflows)
ENGINES = {
    "ghidra":        {"event": "decompile-job",              "ext": [".exe", ".dll", ".so", ".elf", ".bin", ".o", ".dylib", ".macho", ".zip", ".apk"], "premium": False},
    "jadx":          {"event": "decompile-jadx",             "ext": [".apk", ".dex", ".smali", ".zip"], "premium": False},
    "dex2jar":       {"event": "decompile-dex2jar",          "ext": [".apk", ".dex", ".zip"], "premium": False},
    "apktool":       {"event": "decompile-apktool",          "ext": [".apk", ".zip"], "premium": True},
    "apktool-build": {"event": "compile-apktool",            "ext": [".zip"], "premium": True},
    "smali":         {"event": "decompile-smali",            "ext": [".dex", ".zip"], "premium": False},
    "smali-extract": {"event": "decompile-smali-extract",    "ext": [".dex", ".zip"], "premium": False},
    "dex-compile-smali": {"event": "dex-compile-smali",      "ext": [".smali", ".zip"], "premium": True},
    "dex-compile-java":  {"event": "dex-compile-java",       "ext": [".java", ".jar", ".class", ".zip"], "premium": True},
    "cc-compile":    {"event": "cc-compile",                 "ext": [".c", ".cpp", ".zip"], "premium": True},
    "apk-source-build": {"event": "apk-source-build",        "ext": [".zip"], "premium": True},
    "apk-sign":      {"event": "apk-sign",                   "ext": [".apk"], "premium": True},
    "pdf-to-txt":    {"event": "pdf-to-txt",                 "ext": [".pdf", ".zip"], "premium": False},
}

# Temporary file lifetime for RE jobs
JOB_FILE_TTL_HOURS = 24
