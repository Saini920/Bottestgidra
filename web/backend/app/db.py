"""SQLite data layer (stdlib sqlite3)."""
import sqlite3
import threading
from datetime import datetime, date

from . import config, security

_lock = threading.Lock()
_conn = None


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema()
    return _conn


def _init_schema():
    with _lock:
        cur = _conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                session_enc TEXT DEFAULT '',
                created_at TEXT,
                is_admin INTEGER DEFAULT 0,
                is_premium INTEGER DEFAULT 0,
                premium_until TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                msg_id INTEGER,
                filename TEXT,
                size INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                engine TEXT,
                filename TEXT,
                status TEXT DEFAULT 'queued',
                gh_run_id TEXT DEFAULT '',
                error TEXT DEFAULT '',
                result_path TEXT DEFAULT '',
                created_at TEXT,
                finished_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS usage (
                user_id INTEGER,
                day TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, day)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_enc TEXT,
                updated_at TEXT
            );
            """
        )
        _conn.commit()


# ---------- users ----------

def upsert_user(user_id: int, name: str = "", username: str = "", phone: str = "", session_enc: str = ""):
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            """INSERT INTO users (user_id, name, username, phone, session_enc, created_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name=excluded.name, username=excluded.username, phone=excluded.phone,
                 session_enc=excluded.session_enc""",
            (user_id, name, username, phone, session_enc, _now()),
        )
        get_conn().commit()


def get_user(user_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def clear_session(user_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("UPDATE users SET session_enc='' WHERE user_id=?", (user_id,))
        get_conn().commit()


def count_users() -> int:
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT COUNT(*) AS n FROM users")
        return cur.fetchone()["n"]


def count_admins() -> int:
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE is_admin=1")
        return cur.fetchone()["n"]


def set_admin(user_id: int, is_admin: int = 1):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("UPDATE users SET is_admin=? WHERE user_id=?", (is_admin, user_id))
        get_conn().commit()


# ---------- settings (encrypted at rest) ----------

SETTING_KEYS = ("API_ID", "API_HASH", "GITHUB_TOKEN", "GITHUB_REPO", "PUBLIC_URL",
                "WEBHOOK_TOKEN", "TELEGRAM_BOT_TOKEN", "ADMIN_IDS")


def set_setting(key: str, value: str):
    if key not in SETTING_KEYS:
        return False
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            "INSERT INTO settings (key, value_enc, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_enc=excluded.value_enc, updated_at=excluded.updated_at",
            (key, security.encrypt(value or ""), _now()),
        )
        get_conn().commit()
    return True


def get_setting(key: str):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT value_enc FROM settings WHERE key=?", (key,))
        row = cur.fetchone()
    if not row:
        return None
    val = security.decrypt(row["value_enc"])
    return val or None


def all_settings() -> dict:
    out = {}
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT key, value_enc FROM settings")
        rows = cur.fetchall()
    for r in rows:
        val = security.decrypt(r["value_enc"])
        if val:
            out[r["key"]] = val
    return out


# ---------- usage ----------

def add_usage(user_id: int, count: int = 1):
    today = date.today().isoformat()
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            "INSERT INTO usage (user_id, day, count) VALUES (?,?,?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count = count + ?",
            (user_id, today, count, count),
        )
        get_conn().commit()


def get_usage(user_id: int) -> int:
    today = date.today().isoformat()
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, today))
        row = cur.fetchone()
        return row["count"] if row else 0


# ---------- files ----------

def add_file(user_id: int, msg_id: int, filename: str, size: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            "INSERT INTO files (user_id, msg_id, filename, size, created_at) VALUES (?,?,?,?,?)",
            (user_id, msg_id, filename, size, _now()),
        )
        get_conn().commit()
        return cur.lastrowid


def list_files(user_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT * FROM files WHERE user_id=? ORDER BY created_at DESC", (user_id,))
        return [dict(r) for r in cur.fetchall()]


def get_file(user_id: int, file_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT * FROM files WHERE id=? AND user_id=?", (file_id, user_id))
        row = cur.fetchone()
        return dict(row) if row else None


def delete_file(user_id: int, file_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("DELETE FROM files WHERE id=? AND user_id=?", (file_id, user_id))
        get_conn().commit()


# ---------- jobs ----------

def create_job(user_id: int, engine: str, filename: str) -> int:
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            "INSERT INTO jobs (user_id, engine, filename, status, created_at) VALUES (?,?,?,?,?)",
            (user_id, engine, filename, "queued", _now()),
        )
        get_conn().commit()
        return cur.lastrowid


def update_job(job_id: int, **fields):
    allowed = {"status", "gh_run_id", "error", "result_path", "finished_at"}
    sets = [f"{k}=?" for k in fields if k in allowed]
    if not sets:
        return
    with _lock:
        cur = get_conn().cursor()
        cur.execute(
            f"UPDATE jobs SET {', '.join(sets)} WHERE id=?",
            [fields[k] for k in fields if k in allowed] + [job_id],
        )
        get_conn().commit()


def get_job(job_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def list_jobs(user_id: int):
    with _lock:
        cur = get_conn().cursor()
        cur.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user_id,))
        return [dict(r) for r in cur.fetchall()]
