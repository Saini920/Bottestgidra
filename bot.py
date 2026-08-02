import asyncio
import logging
import os
import re
import sys
import time

from datetime import date
import httpx
from telegram import Update, constants, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ghidra-bot")

SCRIPT_VERSION = "v4-gh"
log.info("ghidra-bot %s starting (GitHub Actions worker)", SCRIPT_VERSION)

from pathlib import Path

env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_FILE_MB = int(os.environ.get("MAX_FILE_MB", "100"))
MAX_CONCURRENT_JOBS = 10
MAX_DAILY_FILES = 30
ADMIN_IDS = ["6684870256", "7251749429"]
ALLOWED_USERS = [u.strip() for u in os.environ.get("ALLOWED_USER_IDS", "").split(",") if u.strip()]
APPROVED_USERS = set(ALLOWED_USERS + ADMIN_IDS)
BANNED_USERS = set()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Saini920/Bottestgidra")
GITHUB_EVENT = os.environ.get("GITHUB_EVENT", "decompile-job")


def is_allowed(user_id: int) -> bool:
    uid = str(user_id)
    if uid in BANNED_USERS:
        return False
    return not APPROVED_USERS or uid in APPROVED_USERS

job_queue = asyncio.Queue()
active_jobs_timestamps = []
daily_usage = {}  # {user_id: {"date": date_obj, "count": int}}


def check_daily_limit(user_id: int) -> str | None:
    today = date.today()
    record = daily_usage.get(user_id)
    if record and record["date"] == today:
        if record["count"] >= MAX_DAILY_FILES:
            return f"⚠️ <b>Daily Limit Reached!</b>\nYou have reached your daily quota of <b>{MAX_DAILY_FILES} files</b>. Further uploads will be permitted tomorrow."
        record["count"] += 1
    else:
        daily_usage[user_id] = {"date": today, "count": 1}
    return None


async def enqueue_or_dispatch(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = ""):
    now = time.time()
    active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]
    if len(active_jobs_timestamps) >= MAX_CONCURRENT_JOBS:
        pos = job_queue.qsize() + 1
        await status.edit_text(
            f"⏳ <b>Server Busy! Task Queued (#Position {pos})</b>\n"
            f"All active worker slots ({MAX_CONCURRENT_JOBS}/{MAX_CONCURRENT_JOBS}) are currently occupied.\n"
            "Decompilation will start automatically as soon as a slot opens.",
            parse_mode=constants.ParseMode.HTML,
        )
        await job_queue.put((msg, status, file_url, filename, tg_file_path))
    else:
        active_jobs_timestamps.append(now)
        await send_to_job(msg, status, file_url, filename, tg_file_path)


async def queue_worker_loop():
    while True:
        try:
            item = await job_queue.get()
            msg, status, file_url, filename, tg_file_path = item
            now = time.time()
            active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]
            while len(active_jobs_timestamps) >= MAX_CONCURRENT_JOBS:
                await asyncio.sleep(5)
                now = time.time()
                active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]

            active_jobs_timestamps.append(now)
            await send_to_job(msg, status, file_url, filename, tg_file_path)
            job_queue.task_done()
        except Exception as e:
            log.exception("Queue worker error", exc_info=e)
            await asyncio.sleep(1)


OVER_LIMIT_MSG = (
    "⚠️ <b>File size limit exceeded!</b>\n"
    "Max Telegram upload for bot is <b>100 MB</b> (file is {size:.1f} MB).\n\n"
    "Use the <b>link method</b> for larger files:\n"
    "   /link <i>https://your-link.com/file.so</i>\n\n"
    "Powered By @Ghostofhackers"
)


ACCESS_DENIED_MSG = (
    "🔒 <b>Access Denied</b>\n\n"
    "This bot is private and restricted to approved users only.\n"
    "Contact an Admin or click the button below to request access.\n\n"
    "👥 <b>Admins:</b> @R3V_X | @Ghostofhackers"
)


def is_allowed(user_id: int) -> bool:
    return not APPROVED_USERS or str(user_id) in APPROVED_USERS


async def reply_denied(msg) -> None:
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📩 Request Access", callback_data="req_access"),
            InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers"),
        ]
    ])
    await msg.reply_text(ACCESS_DENIED_MSG, parse_mode=constants.ParseMode.HTML, reply_markup=keyboard)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    data = query.data

    if data == "req_access":
        await query.answer("📩 Access request sent to Admins!", show_alert=True)
        admin_text = (
            "🔔 <b>NEW ACCESS REQUEST</b>\n"
            "═══════════════════════\n"
            f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
            f"👤 <b>Name:</b> {user.full_name}\n"
            f"🌐 <b>Username:</b> @{user.username if user.username else 'N/A'}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}"),
                InlineKeyboardButton("❌ Deny", callback_data=f"deny_{user.id}"),
            ]
        ])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=int(admin_id), text=admin_text, parse_mode=constants.ParseMode.HTML, reply_markup=keyboard
                )
            except Exception as e:
                log.warning("Failed to send admin notification to %s: %s", admin_id, e)
        return

    if str(user.id) not in ADMIN_IDS:
        await query.answer("❌ Only Admins can perform this action!", show_alert=True)
        return

    if data.startswith("app_"):
        target_id = data.split("app_")[1]
        APPROVED_USERS.add(target_id)
        await query.answer("✅ User Approved!", show_alert=True)
        admin_name = f"@{user.username}" if user.username else user.first_name
        await query.edit_message_text(
            f"✅ <b>Approved User <code>{target_id}</code></b>\nApproved by {admin_name}",
            parse_mode=constants.ParseMode.HTML,
        )
        try:
            user_msg = (
                "🎉 <b>Access Approved!</b>\n\n"
                "Your request for bot access has been approved by the Admin.\n"
                "You can now send files or commands to start decompiling!"
            )
            await context.bot.send_message(chat_id=int(target_id), text=user_msg, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("Could not notify user %s: %s", target_id, e)

    elif data.startswith("deny_"):
        target_id = data.split("deny_")[1]
        await query.answer("❌ Request Declined!", show_alert=True)
        admin_name = f"@{user.username}" if user.username else user.first_name
        await query.edit_message_text(
            f"❌ <b>Declined User <code>{target_id}</code></b>\nDeclined by {admin_name}",
            parse_mode=constants.ParseMode.HTML,
        )
        try:
            user_msg = (
                "❌ <b>Access Denied</b>\n\n"
                "Your request for bot access was declined by the Admin."
            )
            await context.bot.send_message(chat_id=int(target_id), text=user_msg, parse_mode=constants.ParseMode.HTML)
        except Exception as e:
            log.warning("Could not notify user %s: %s", target_id, e)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message)
        return
    await update.message.reply_text(
        "🤖 Welcome to Ghidra Decompiler Bot!\n\n"
        "🔬 This bot uses <b>Ghidra</b> (NSA's reverse engineering framework) on a "
        "<b>7GB RAM GitHub super server</b> — 100% FREE, no size limits!\n\n"
        "📦 <b>What you get back:</b>\n"
        "  • decompiled.c — full C code of every function 🧠\n"
        "  • info.txt — strings, symbols, compiler, architecture 📊\n"
        "  • Delivered as one neat ZIP file 📂\n\n"
        "═══════════════════════\n"
        "📤 <b>Method 1: Direct upload</b>\n"
        "Just send the file directly:\n"
        "  • .exe / .dll / .so / .elf / .apk / .zip\n"
        "  ⚠️ Max <b>" + str(MAX_FILE_MB) + " MB</b> (Telegram bot limit)\n\n"
        "═══════════════════════\n"
        "🔗 <b>Method 2: Link method (no size limit!)</b>\n"
        "For bigger files:\n"
        "  <b>Step 1:</b> Upload file to Google Drive / MediaFire / "
        "Dropbox / GitHub / any host\n"
        "  <b>Step 2:</b> Copy the shareable link\n"
        "  <b>Step 3:</b> Send: <i>/link &lt;url&gt;</i>\n"
        "  ✅ ZIP / APK / JAR are auto-extracted, binaries inside are found "
        "and decompiled\n\n"
        "⚡ <b>Features:</b>\n"
        "  • Full decompilation (Ghidra engine, 7GB RAM)\n"
        "  • Function-by-function C reconstruction\n"
        "  • String & symbol extraction\n"
        "  • ELF / PE / Mach-O / Android APK support\n"
        "  • Live progress animation (0-100%)\n\n"
        "🚀 Send a file or a link now! Powered By @Ghostofhackers",
        parse_mode=constants.ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message)
        return
    help_text = (
        "🤖 <b>GHIDRA DECOMPILER BOT — HELP & COMMANDS</b>\n"
        "═══════════════════════════════════\n"
        "<b>Description:</b>\n"
        "This bot decompiles binary executables (.exe, .dll, .so, .elf, .apk, .zip) into readable C source code and extracts symbol/string metadata using NSA's <b>Ghidra Engine</b> running on <b>GitHub's 7GB RAM Cloud Server</b>.\n\n"
        "📌 <b>ALL AVAILABLE COMMANDS:</b>\n"
        "• <code>/start</code> — Welcome guide and basic usage.\n"
        "• <code>/help</code> — View all commands and bot description.\n"
        "• <code>/profile</code> — View your profile, daily remaining quota, and server stats.\n"
        "• <code>/myid</code> — Display your Telegram User ID.\n"
        "• <code>/link &lt;url&gt;</code> — Decompile large files via direct link (Google Drive, MediaFire, Dropbox, etc.).\n\n"
        "📤 <b>DIRECT UPLOAD:</b>\n"
        "• Send any binary file directly in chat (Max <b>100 MB</b>).\n\n"
        "📊 <b>BOT LIMITS & RULES:</b>\n"
        "• <b>Max Direct Upload:</b> 100 MB per file\n"
        "• <b>Daily Quota:</b> 30 files / day per user\n"
        "• <b>Server Concurrency:</b> Max 10 active jobs at a time\n\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.HTML)


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your Telegram User ID:\n<code>{update.effective_user.id}</code>", parse_mode=constants.ParseMode.HTML)


async def trigger_github(file_url: str, chat_id: int, message_id: int, filename: str, tg_file_path: str = "") -> bool:
    if not GITHUB_TOKEN:
        return False
    client_payload = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "filename": filename,
        "bot_token": BOT_TOKEN,
    }
    if tg_file_path:
        client_payload["tg_file_path"] = tg_file_path
    else:
        client_payload["file_url"] = file_url
    payload = {"event_type": GITHUB_EVENT, "client_payload": client_payload}
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"https://api.github.com/repos/{GITHUB_REPO}/dispatches",
            headers={
                "Authorization": f"Bearer {GITHUB_TOKEN}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "ghidra-bot",
            },
            json=payload,
        )
    log.info("dispatch status=%s body=%s", resp.status_code, resp.text[:200])
    return resp.status_code in (204, 200)


async def send_to_job(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = ""):
    if not GITHUB_TOKEN:
        await status.edit_text(
            "❌ GitHub trigger failed: <b>GITHUB_TOKEN env missing</b> on Railway.\n"
            "Set it in Railway Dashboard → Variables, then Redeploy.\n"
            "Powered By @Ghostofhackers",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    if not await trigger_github(file_url, msg.chat_id, status.message_id, filename, tg_file_path):
        await status.edit_text(
            "❌ GitHub trigger failed: GitHub API ne dispatch reject kiya.\n"
            "Check that GITHUB_TOKEN is correct (repo scope) and repo is "
            "<code>Toboisking/ghidra-telegram-bot</code>.",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    await status.edit_text(
        "Job sent to server!\n"
        "⏱️ Expected: 2-10 minutes.\n"
        "▰▱▱▱▱▱▱▱▱▱▱▱▱▱▱▱ 0.00 %",
        parse_mode=constants.ParseMode.HTML,
    )


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message)
        return

    msg = update.message
    doc = msg.document
    if doc is None:
        await msg.reply_text("📄 Send a file (document) — EXE, DLL, SO, ELF, APK etc.")
        return

    err = check_daily_limit(update.effective_user.id)
    if err:
        await msg.reply_text(err, parse_mode=constants.ParseMode.HTML)
        return

    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        await msg.reply_text(OVER_LIMIT_MSG.format(size=size_mb), parse_mode=constants.ParseMode.HTML)
        return

    status = await msg.reply_text("🚀 File received! Sending to GitHub server...")

    try:
        tg_file = await doc.get_file()
        tg_file_path = tg_file.file_path
        if not tg_file_path:
            await status.edit_text("❌ Could not get file path from Telegram. Try the /link method.")
            return

        await enqueue_or_dispatch(msg, status, filename=doc.file_name, tg_file_path=tg_file_path)
    except Exception as e:
        await status.edit_text("❌ File processing failed: " + str(e))


async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message)
        return

    if not context.args:
        await update.message.reply_text(
            "🔗 Usage: /link <url>\n\n"
            "Example:\n"
            "/link https://drive.google.com/file/d/ABC123/view\n"
            "/link https://example.com/firmware.bin\n\n"
            "Supported: Google Drive, MediaFire, Dropbox, GitHub, any direct link."
        )
        return

    url = context.args[0].strip()
    if not url.startswith(("http://", "https://")):
        await update.message.reply_text("❌ Send a valid link (http:// or https://).")
        return

    err = check_daily_limit(update.effective_user.id)
    if err:
        await update.message.reply_text(err, parse_mode=constants.ParseMode.HTML)
        return

    msg = update.message
    status = await msg.reply_text("🔗 Link received! Sending to GitHub server...")
    filename = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download"
    await enqueue_or_dispatch(msg, status, url, str(filename))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await reply_denied(update.message)
        return

    today = date.today()
    record = daily_usage.get(user.id)
    used_today = record["count"] if (record and record["date"] == today) else 0
    remaining = max(0, MAX_DAILY_FILES - used_today)

    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])

    profile_text = (
        "👤 <b>USER PROFILE & STATS</b>\n"
        "═══════════════════════\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Name:</b> {user.full_name}\n"
        f"🌐 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"✅ <b>Status:</b> Approved User\n\n"
        "📊 <b>USAGE & LIMITS</b>\n"
        "───────────────────────\n"
        f"📅 <b>Today's Files Used:</b> {used_today} / {MAX_DAILY_FILES}\n"
        f"🔄 <b>Remaining Today:</b> {remaining} files\n"
        f"⚡ <b>Max Direct Upload:</b> {MAX_FILE_MB} MB\n"
        f"⚙️ <b>Server Active Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(profile_text, parse_mode=constants.ParseMode.HTML)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve <user_id>")
        return
    target_id = context.args[0].strip()
    APPROVED_USERS.add(target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> has been approved.", parse_mode=constants.ParseMode.HTML)
    try:
        await context.bot.send_message(chat_id=int(target_id), text="🎉 <b>Access Approved!</b>\nYour access request has been approved by the Admin.", parse_mode=constants.ParseMode.HTML)
    except Exception:
        pass


async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unapprove <user_id>")
        return
    target_id = context.args[0].strip()
    APPROVED_USERS.discard(target_id)
    await update.message.reply_text(f"❌ User <code>{target_id}</code> has been unapproved/revoked.", parse_mode=constants.ParseMode.HTML)
    try:
        await context.bot.send_message(chat_id=int(target_id), text="🔒 <b>Access Revoked</b>\nYour access to the bot has been revoked by the Admin.", parse_mode=constants.ParseMode.HTML)
    except Exception:
        pass


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    target_id = context.args[0].strip()
    BANNED_USERS.add(target_id)
    APPROVED_USERS.discard(target_id)
    await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode=constants.ParseMode.HTML)
    try:
        await context.bot.send_message(chat_id=int(target_id), text="🚫 <b>Account Banned</b>\nYou have been banned from using this bot.", parse_mode=constants.ParseMode.HTML)
    except Exception:
        pass


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    target_id = context.args[0].strip()
    BANNED_USERS.discard(target_id)
    await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode=constants.ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message text>")
        return
    broadcast_msg = update.message.text.split(None, 1)[1]
    target_users = set(list(APPROVED_USERS) + list(daily_usage.keys()))
    sent, failed = 0, 0
    status_msg = await update.message.reply_text("📢 Broadcasting message...")
    for uid in target_users:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 <b>ANNOUNCEMENT:</b>\n\n{broadcast_msg}",
                parse_mode=constants.ParseMode.HTML,
            )
            sent += 1
        except Exception:
            failed += 1
    await status_msg.edit_text(
        f"✅ <b>Broadcast Complete!</b>\n"
        f"📤 Sent: {sent}\n"
        f"❌ Failed: {failed}",
        parse_mode=constants.ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])
    today = date.today()
    today_files = sum(rec["count"] for rec in daily_usage.values() if rec.get("date") == today)
    stats_text = (
        "📊 <b>ADMIN SYSTEM STATS</b>\n"
        "═══════════════════════\n"
        f"👥 <b>Approved Users:</b> {len(APPROVED_USERS)}\n"
        f"🚫 <b>Banned Users:</b> {len(BANNED_USERS)}\n"
        f"📅 <b>Total Files Processed Today:</b> {today_files}\n"
        f"⚙️ <b>Active Cloud Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers</i>"
    )
    await update.message.reply_text(stats_text, parse_mode=constants.ParseMode.HTML)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error", exc_info=context.error)


async def post_init(app: Application):
    asyncio.create_task(queue_worker_loop())


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN env set nahi hai!")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("unapprove", cmd_unapprove))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(MessageHandler(filters.ATTACHMENT, handle_file))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_error_handler(error_handler)

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    if WEBHOOK_URL:
        log.info("Webhook mode: %s", WEBHOOK_URL)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=WEBHOOK_URL + "/" + BOT_TOKEN,
        )
    else:
        log.info("Polling mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
