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

import json
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

PENDING_REQUESTS = set()
ADMIN_STATE = {}  # {user_id: state_str}
ADMIN_TEMP_DATA = {}

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Saini920/Bottestgidra")
GITHUB_EVENT = os.environ.get("GITHUB_EVENT", "decompile-job")

from database import RepoDB
db = RepoDB(GITHUB_TOKEN, GITHUB_REPO)

def record_user_name(user):
    uid = str(user.id)
    name = user.first_name
    if user.username:
        name += f" (@{user.username})"
    if db.get_name(uid) != name:
        db.data["names"][uid] = name
        db.save()



FORCE_CHANNELS = ["-1002378157598", "-1003121382577"]

async def check_force_join(update, context) -> bool:
    uid = update.effective_user.id
    record_user_name(update.effective_user)
    if str(uid) in ADMIN_IDS: return True
    for ch in FORCE_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=uid)
            if member.status in ["left", "kicked"]:
                raise Exception("Not member")
        except Exception as e:
            log.warning(f"Force join check failed for {ch}: {e}")
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel 1", url="https://t.me/allinformation0173")],
                [InlineKeyboardButton("Join Channel 2", url="https://t.me/+gQawrH0MFs00M2Y1")]
            ])
            try:
                await update.message.reply_text("❌ <b>You must join our channels to use this bot!</b>\nJoin the channels and try again.", reply_markup=keyboard, parse_mode="HTML")
            except: pass
            return False
    return True

def is_allowed(user_id: int) -> bool:
    uid = str(user_id)
    if uid in db.data["banned"]:
        return False
    # Admins and allowed users from ENV bypass approval
    if uid in ADMIN_IDS or uid in ALLOWED_USERS:
        return True
    if db.data.get("free_mode", False):
        return True
    return uid in db.data["approved"]

job_queue = asyncio.Queue()
active_jobs_timestamps = []

from datetime import date, timedelta


def check_daily_limit(user_id: int) -> str | None:
    uid = str(user_id)
    if uid in ADMIN_IDS:
        return None  # Admins have NO limits!

    today = date.today()
    today_iso = today.isoformat()
    sub = db.data["subscriptions"].get(uid)
    if sub:
        try:
            exp_date = date.fromisoformat(sub["expires_at"])
            if today > exp_date:
                db.remove_approved(uid)
                db.remove_sub(uid)
                
                
                return "⚠️ <b>Access Expired!</b>\nYour custom subscription period has ended. Please contact Admin to renew."
            user_max_files = sub.get("daily_limit", MAX_DAILY_FILES)
        except Exception:
            user_max_files = MAX_DAILY_FILES
    else:
        user_max_files = MAX_DAILY_FILES

    record = db.data['daily_usage'].get(uid)
    if record and record["date"] == today_iso:
        if record["count"] >= user_max_files:
            return f"⚠️ <b>Daily Limit Reached!</b>\nYou have reached your daily quota of <b>{user_max_files} files</b>. Further uploads will be permitted tomorrow."
        record["count"] += 1
    else:
        db.data['daily_usage'][uid] = {"date": today_iso, "count": 1}
    db.save()
    return None


async def enqueue_or_dispatch(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = ""):
    user_id = str(msg.from_user.id) if msg and msg.from_user else ""
    now = time.time()
    active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]

    is_admin = user_id in ADMIN_IDS
    is_priority = is_admin or user_id in db.data["subscriptions"]

    if is_priority or len(active_jobs_timestamps) < MAX_CONCURRENT_JOBS:
        active_jobs_timestamps.append(now)
        await send_to_job(msg, status, file_url, filename, tg_file_path, is_admin)
    else:
        pos = job_queue.qsize() + 1
        priority_label = "⚡ <b>Priority Fast-Lane Slot Granted!</b>\n" if is_priority else ""
        await status.edit_text(
            f"⏳ <b>Server Busy! Task Queued (#Position {pos})</b>\n"
            f"{priority_label}"
            f"All active worker slots ({MAX_CONCURRENT_JOBS}/{MAX_CONCURRENT_JOBS}) are occupied.\n"
            "Decompilation will start automatically as soon as a slot opens.",
            parse_mode=constants.ParseMode.HTML,
        )
        await job_queue.put((msg, status, file_url, filename, tg_file_path, is_admin))


async def queue_worker_loop():
    while True:
        try:
            item = await job_queue.get()
            if len(item) == 5:
                msg, status, file_url, filename, tg_file_path = item
                is_admin = False
            else:
                msg, status, file_url, filename, tg_file_path, is_admin = item
            now = time.time()
            active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]
            while len(active_jobs_timestamps) >= MAX_CONCURRENT_JOBS:
                await asyncio.sleep(5)
                now = time.time()
                active_jobs_timestamps[:] = [t for t in active_jobs_timestamps if now - t < 600]

            active_jobs_timestamps.append(now)
            await send_to_job(msg, status, file_url, filename, tg_file_path, is_admin)
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



FORCE_CHANNELS = ["@allinformation0173"]
try:
    if os.environ.get("FORCE_CHANNEL_2"):
        FORCE_CHANNELS.append(os.environ.get("FORCE_CHANNEL_2"))
except: pass

async def check_force_join(update, context) -> bool:
    uid = update.effective_user.id
    record_user_name(update.effective_user)
    if str(uid) in ADMIN_IDS: return True
    for ch in FORCE_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=uid)
            if member.status in ["left", "kicked"]:
                raise Exception("Not member")
        except Exception:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel 1", url="https://t.me/allinformation0173")],
                [InlineKeyboardButton("Join Channel 2", url="https://t.me/+gQawrH0MFs00M2Y1")]
            ])
            try:
                await update.message.reply_text("❌ <b>You must join our channels to use this bot!</b>\nJoin the channels and try again.", reply_markup=keyboard, parse_mode="HTML")
            except: pass
            return False
    return True



async def reply_denied(msg, user_id: int = None) -> None:
    uid = str(user_id) if user_id else ""
    if uid and uid in PENDING_REQUESTS:
        text = (
            "⏳ <b>Access Request Pending</b>\n\n"
            "Your access request has been submitted to the Admins (@Ghostofhackers & @R3V_X).\n"
            "Please wait for an Admin to review and approve your request."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers")]
        ])
    else:
        text = ACCESS_DENIED_MSG
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📩 Request Access", callback_data="req_access"),
                InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers"),
            ]
        ])
    await msg.reply_text(text, parse_mode=constants.ParseMode.HTML, reply_markup=keyboard)


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    record_user_name(user)
    data = query.data

    if data == "buy_sub":
        await query.answer("⭐ Ghidra Decompiler Premium Plan (₹99)", show_alert=False)
        sub_details = (
            "⭐ <b>GHIDRA DECOMPILER — PREMIUM SUBSCRIPTION</b>\n"
            "═══════════════════════════════════\n"
            "💳 <b>PRICE:</b> <b>₹99 ONLY</b>\n\n"
            "⚡ <b>PREMIUM BENEFITS & FEATURES:</b>\n"
            "• 📊 <b>Increased Daily Limit:</b> <b>70 Files / Day</b> (vs 30 Free)\n"
            "• 📦 <b>Direct File Upload Limit:</b> <b>100 MB</b> (vs 20 MB Free)\n"
            "• 🔗 <b>Direct Link Method (/link):</b> Unlimited size decompilation\n"
            "• 🚀 <b>Priority Fast-Lane Queue:</b> Instant processing during peak load\n"
            "• 📦 <b>Batch ZIP Decompiler:</b> Upload up to <b>5 binaries in 1 ZIP</b>\n"
            "• 🔔 <b>Expiry Warnings:</b> Advance 5-day & 1-day renewal alerts\n"
            "• 🛠️ <b>Dedicated Priority Support</b>\n\n"
            "═══════════════════════════════════\n"
            "💳 <b>BUY / RENEW SUBSCRIPTION (₹99):</b>\n"
            "Contact Admins to upgrade your account:\n"
            "👤 <b>Admin 1:</b> @Ghostofhackers\n"
            "👤 <b>Admin 2:</b> @R3V_X"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("💬 Contact @Ghostofhackers (₹99)", url="https://t.me/Ghostofhackers"),
                InlineKeyboardButton("💬 Contact @R3V_X (₹99)", url="https://t.me/R3V_X"),
            ]
        ])
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=sub_details,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as e:
            log.warning("Could not send buy_sub message to user %s: %s", user.id, e)
        return

    if data == "req_access":
        uid = str(user.id)
        if uid in PENDING_REQUESTS:
            await query.answer("⏳ Your access request is already pending Admin approval!", show_alert=True)
            return
        PENDING_REQUESTS.add(uid)
        await query.answer("📩 Access request sent to Admins!", show_alert=True)
        try:
            await query.edit_message_text(
                "⏳ <b>Access Request Pending</b>\n\n"
                "Your access request has been submitted to the Admins (@Ghostofhackers & @R3V_X).\n"
                "You will receive a notification as soon as an Admin approves your request.",
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👤 Contact Admin", url="https://t.me/Ghostofhackers")]
                ])
            )
        except Exception:
            pass

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
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
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
        PENDING_REQUESTS.discard(target_id)
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



async def cmd_list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS: return
    cmd = update.message.text.split()[0].lower()
    
    if cmd == "/approved_users":
        users = db.data["approved"]
        title = "👥 Approved Users"
    elif cmd == "/unapproved_users":
        users = [] # Need to fetch from bot history or PENDING_REQUESTS? We only have pending.
        users = list(PENDING_REQUESTS)
        title = "⏳ Pending Users"
    elif cmd == "/ban_users" or cmd == "/banned_users":
        users = db.data["banned"]
        title = "🚫 Banned Users"
    elif cmd == "/premium_users":
        users = list(db.data["subscriptions"].keys())
        title = "⭐ Premium Users"
    else: return
    
    text = f"<b>{title} ({len(users)}):</b>\n"
    for u in users:
        name = db.get_name(u)
        if name == "Unknown":
            name = ""
        else:
            name = f" - {name}"
        text += f"• <code>{u}</code>{name}\n"
    if not users: text += "None found."
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_force_join(update, context): return
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return
    await update.message.reply_text(
        "🤖 Welcome to Ghidra Decompiler Bot!\n\n"
        "🔬 This bot uses <b>Ghidra</b> (NSA's reverse engineering framework) on a "
        "<b>High Performance Cloud Server</b>!\n\n"
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
        "  • Full decompilation (Ghidra engine)\n"
        "  • Function-by-function C reconstruction\n"
        "  • String & symbol extraction\n"
        "  • ELF / PE / Mach-O / Android APK support\n"
        "  • Live progress animation (0-100%)\n\n"
        "⭐ <b>PREMIUM SUBSCRIPTION & UPGRADE (₹99):</b>\n"
        "  • 🆓 <b>Free Quota:</b> 30 Files / Day (Max 20 MB Upload)\n"
        "  • ⭐ <b>Premium Quota:</b> 70 Files / Day (Max 100 MB Upload + /link method)\n"
        "  • 🚀 <b>Priority Fast-Lane Queue Slot</b> (Skip waiting queue)\n"
        "  • 📦 <b>Multi-File Batch ZIP Decompiler</b> (Up to 5 files/ZIP)\n"
        "  • 💳 <b>Price:</b> <b>₹99 Only</b>\n"
        "  • 💬 <b>To Buy/Renew:</b> Contact @Ghostofhackers | @R3V_X\n\n"
        "🚀 Send a file or a link now! Powered By @Ghostofhackers & @R3V_X",
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Buy Premium Plan (₹99)", callback_data="buy_sub")]
        ])
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return

    admin_section = ""
    if user_id in ADMIN_IDS:
        admin_section = (
            "\n\n👑 <b>ADMIN COMMANDS:</b>\n"
            "• <code>/approve</code> — Approve user access (interactive or <code>/approve &lt;id&gt;</code>)\n"
            "• <code>/unapprove</code> — Revoke user access (interactive or <code>/unapprove &lt;id&gt;</code>)\n"
            "• <code>/ban</code> — Ban user from bot (interactive or <code>/ban &lt;id&gt;</code>)\n"
            "• <code>/unban</code> — Unban user (interactive or <code>/unban &lt;id&gt;</code>)\n"
            "• <code>/setlimit</code> — Set custom limit & days (interactive or <code>/setlimit &lt;id&gt; &lt;limit&gt; &lt;days&gt;</code>)\n"
            "• <code>/broadcast</code> — Broadcast message to all users (interactive or <code>/broadcast &lt;msg&gt;</code>)\n"
            "• <code>/stats</code> — View complete admin system statistics\n"
            "\n👥 <b>USER LISTS:</b>\n"
            "• <code>/approved_users</code> — List all approved users\n"
            "• <code>/unapproved_users</code> — List pending approval requests\n"
            "• <code>/ban_users</code> — List all banned users\n"
            "• <code>/premium_users</code> — List premium subscribers\n"
        )

    help_text = (
        "🤖 <b>GHIDRA DECOMPILER BOT — HELP & COMMANDS</b>\n"
        "═══════════════════════════════════\n"
        "<b>Description:</b>\n"
        "This bot decompiles binary executables (.exe, .dll, .so, .elf, .apk, .zip) into readable C source code and extracts symbol/string metadata using NSA's <b>Ghidra Engine</b> running on <b>Cloud Server</b>.\n\n"
        "📌 <b>USER COMMANDS:</b>\n"
        "• <code>/start</code> — Welcome guide and basic usage.\n"
        "• <code>/help</code> — View all commands and bot description.\n"
        "• <code>/profile</code> — View your profile, daily remaining quota, and server stats.\n"
        "• <code>/myid</code> — Display your Telegram User ID.\n"
        "• <code>/link &lt;url&gt;</code> — Decompile large files via direct link (Google Drive, MediaFire, Dropbox, etc.)."
        f"{admin_section}\n\n"
        "⭐ <b>PREMIUM SUBSCRIPTION BENEFITS (₹99):</b>\n"
        "• 🆓 <b>Free Quota:</b> 30 files / day (Max 20 MB upload limit)\n"
        "• ⭐ <b>Premium Quota:</b> 70 files / day (Max 100 MB upload limit)\n"
        "• 🔗 <b>Direct Link Method (/link):</b> Exclusive Premium Feature\n"
        "• 🚀 <b>Priority Fast-Lane Queue:</b> Instant execution during peak load\n"
        "• 📦 <b>Batch Decompiler:</b> Upload & decompile up to 5 binaries per ZIP\n"
        "• 🔔 <b>Expiry Alerts:</b> Automated 5-day & 1-day warning alerts\n\n"
        "💳 <b>BUY SUBSCRIPTION (₹99):</b>\n"
        "Contact Admins: <b>@Ghostofhackers</b> | <b>@R3V_X</b>\n\n"
        "📤 <b>DIRECT UPLOAD:</b>\n"
        "• Send any binary file directly in chat (Max <b>100 MB</b> for Premium, Unlimited for Admins).\n\n"
        "📊 <b>BOT LIMITS & RULES:</b>\n"
        "• <b>Max Direct Upload:</b> 100 MB (Unlimited for Admins)\n"
        "• <b>Daily Quota:</b> 30 files / day (Unlimited for Admins)\n"
        "• <b>Server Concurrency:</b> Max 10 active jobs at a time\n\n"
        "⚡ <i>Powered By @Ghostofhackers & @R3V_X</i>"
    )
    await update.message.reply_text(help_text, parse_mode=constants.ParseMode.HTML, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Buy / Upgrade Subscription", callback_data="buy_sub")]
    ]))


async def cmd_myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 Your Telegram User ID:\n<code>{update.effective_user.id}</code>", parse_mode=constants.ParseMode.HTML)


async def trigger_github(file_url: str, chat_id: int, message_id: int, filename: str, tg_file_path: str = "", is_admin: bool = False) -> bool:
    if not GITHUB_TOKEN:
        return False
    client_payload = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "filename": filename,
        "bot_token": BOT_TOKEN,
        "is_admin": str(is_admin),
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


async def send_to_job(msg, status, file_url: str = "", filename: str = "", tg_file_path: str = "", is_admin: bool = False):
    if not GITHUB_TOKEN:
        await status.edit_text(
            "❌ GitHub trigger failed: <b>GITHUB_TOKEN env missing</b> on Railway.\n"
            "Set it in Railway Dashboard → Variables, then Redeploy.\n"
            "Powered By @Ghostofhackers",
            parse_mode=constants.ParseMode.HTML,
        )
        return
    if not await trigger_github(file_url, msg.chat_id, status.message_id, filename, tg_file_path, is_admin):
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
    if not await check_force_join(update, context): return
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
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

    user_id = str(update.effective_user.id)
    is_premium = user_id in ADMIN_IDS or user_id in db.data["subscriptions"]
    user_max_mb = 100 if is_premium else 20

    size_mb = (doc.file_size or 0) / (1024 * 1024)
    if size_mb > user_max_mb:
        if not is_premium:
            limit_msg = (
                "⚠️ <b>File Size Limit Exceeded!</b>\n\n"
                f"Free users can upload files up to <b>20 MB</b> (your file is <b>{size_mb:.1f} MB</b>).\n\n"
                "⭐ Upgrade to <b>Premium (₹99)</b> to upload files up to <b>100 MB</b> and unlock the <b>/link method</b>!"
            )
            await msg.reply_text(
                limit_msg,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⭐ Upgrade to Premium (₹99)", callback_data="buy_sub")]
                ])
            )
        else:
            await msg.reply_text(OVER_LIMIT_MSG.format(size=size_mb), parse_mode=constants.ParseMode.HTML)
        return

    status = await msg.reply_text("🚀 File received! Sending to server...")

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
    if not await check_force_join(update, context): return
    user_id = str(update.effective_user.id)
    if not is_allowed(update.effective_user.id):
        await reply_denied(update.message, update.effective_user.id)
        return

    is_premium = user_id in ADMIN_IDS or user_id in db.data["subscriptions"]
    if not is_premium:
        await update.message.reply_text(
            "🔒 <b>FEATURE RESTRICTED TO PREMIUM SUBSCRIBERS!</b>\n\n"
            "The <b>/link method</b> is an exclusive <b>Premium Feature (₹99)</b>.\n"
            "Free users are restricted to direct file uploads (Max 20 MB).\n"
            "Upgrade your account to decompile large files via direct link without limits!\n\n"
            "💳 <b>Price:</b> <b>₹99</b>\n"
            "👥 Contact Admins to Upgrade: <b>@Ghostofhackers</b> | <b>@R3V_X</b>",
            parse_mode=constants.ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Upgrade to Premium (₹99)", callback_data="buy_sub")]
            ])
        )
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
    status = await msg.reply_text("🔗 Link received! Sending to server...")
    filename = url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "download"
    await enqueue_or_dispatch(msg, status, url, str(filename))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await reply_denied(update.message, user.id)
        return

    today = date.today()
    uid_str = str(user.id)
    sub = db.data["subscriptions"].get(uid_str)

    if uid_str in ADMIN_IDS:
        daily_max = "Unlimited (Admin)"
        sub_info = "⭐ <b>Subscription Plan:</b> Unlimited Admin Access\n"
    elif sub:
        try:
            exp_date = date.fromisoformat(sub["expires_at"])
            days_left = max(0, (exp_date - today).days)
            daily_max = sub.get("daily_limit", MAX_DAILY_FILES)
            sub_info = (
                f"⭐ <b>Subscription Plan:</b> Custom Plan\n"
                f"📊 <b>Custom Daily Quota:</b> {daily_max} files/day\n"
                f"📅 <b>Expiry Date:</b> <code>{sub.get('expires_at')}</code>\n"
                f"⏳ <b>Days Remaining:</b> <b>{days_left} days</b>\n"
            )
        except Exception:
            daily_max = MAX_DAILY_FILES
            sub_info = f"⭐ <b>Subscription Plan:</b> Standard ({MAX_DAILY_FILES} files/day)\n"
    else:
        daily_max = MAX_DAILY_FILES
        sub_info = f"⭐ <b>Subscription Plan:</b> Standard Approved Access\n"

    used_today = record["count"] if ((record := db.data['daily_usage'].get(uid_str)) and record["date"] == today.isoformat()) else 0
    if uid_str in ADMIN_IDS:
        remaining = "Unlimited"
        used_display = f"{used_today} / Unlimited"
        upload_display = "Unlimited (Max Telegram API Limit)"
    else:
        remaining = f"{max(0, daily_max - used_today)} files"
        used_display = f"{used_today} / {daily_max}"
        upload_display = f"{MAX_FILE_MB} MB"

    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
                r1 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=in_progress", headers=headers)
                r2 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=queued", headers=headers)
                if r1.status_code == 200 and r2.status_code == 200:
                    active_now = r1.json().get("total_count", 0) + r2.json().get("total_count", 0)
        except Exception:
            pass

    profile_text = (
        "👤 <b>USER PROFILE & SUBSCRIPTION DETAILS</b>\n"
        "═══════════════════════════════════\n"
        f"🆔 <b>User ID:</b> <code>{user.id}</code>\n"
        f"👤 <b>Name:</b> {user.full_name}\n"
        f"🌐 <b>Username:</b> @{user.username if user.username else 'N/A'}\n"
        f"✅ <b>Status:</b> Approved User\n"
        f"{sub_info}\n"
        "📊 <b>USAGE & LIMITS</b>\n"
        "───────────────────────\n"
        f"📅 <b>Today's Files Used:</b> {used_display}\n"
        f"🔄 <b>Remaining Today:</b> {remaining}\n"
        f"⚡ <b>Max Direct Upload:</b> {upload_display}\n"
        f"⚙️ <b>Server Active Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers & @R3V_X</i>"
    )
    await update.message.reply_text(
        profile_text,
        parse_mode=constants.ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Buy / Upgrade Subscription", callback_data="buy_sub")]
        ])
    )


async def handle_admin_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    if user_id not in ADMIN_IDS or user_id not in ADMIN_STATE:
        return

    state = ADMIN_STATE.pop(user_id)
    text = update.message.text.strip()

    if state == "AWAITING_APPROVE":
        target_id = text
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been approved.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🎉 <b>Access Approved!</b>\nYour request for bot access has been approved by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_UNAPPROVE":
        target_id = text
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"❌ User <code>{target_id}</code> has been unapproved/revoked.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🔒 <b>Access Revoked</b>\nYour access to the bot has been revoked by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_BAN":
        target_id = text
        db.ban(target_id)
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🚫 <b>Account Banned</b>\nYou have been banned from using this bot.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass

    elif state == "AWAITING_UNBAN":
        target_id = text
        db.unban(target_id)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode=constants.ParseMode.HTML)

    elif state == "AWAITING_BROADCAST":
        broadcast_msg = text
        target_users = set(db.data["approved"] + list(db.data['daily_usage'].keys()))
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

    elif state == "AWAITING_SETLIMIT_USERID":
        ADMIN_TEMP_DATA[user_id] = {"target_id": text}
        ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_LIMIT"
        await update.message.reply_text("📊 Please send the <b>Daily File Limit</b> (e.g. 50):", parse_mode=constants.ParseMode.HTML)

    elif state == "AWAITING_SETLIMIT_LIMIT":
        try:
            limit_val = int(text)
            ADMIN_TEMP_DATA[user_id]["daily_limit"] = limit_val
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_DAYS"
            await update.message.reply_text("📅 Please send the <b>Validity Period in Days</b> (e.g. 30):", parse_mode=constants.ParseMode.HTML)
        except ValueError:
            await update.message.reply_text("❌ Invalid limit number! Please enter a valid number (e.g. 50):")
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_LIMIT"

    elif state == "AWAITING_SETLIMIT_DAYS":
        try:
            days_val = int(text)
            temp = ADMIN_TEMP_DATA.pop(user_id, {})
            target_id = temp.get("target_id")
            daily_limit = temp.get("daily_limit", MAX_DAILY_FILES)
            exp_date = (date.today() + timedelta(days=days_val)).isoformat()

            db.set_sub(target_id, exp_date, daily_limit)
            db.add_approved(target_id)

            await update.message.reply_text(
                f"✅ <b>Custom Limit Set Successfully!</b>\n\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"📊 <b>Daily Quota:</b> {daily_limit} files/day\n"
                f"📅 <b>Validity:</b> {days_val} days\n"
                f"⏳ <b>Expires On:</b> {exp_date}",
                parse_mode=constants.ParseMode.HTML,
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "🎉 <b>YOUR SUBSCRIPTION HAS BEEN UPDATED!</b>\n"
                        "═══════════════════════════════════\n"
                        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
                        f"📊 <b>Daily Quota:</b> <b>{daily_limit} files / day</b>\n"
                        f"📅 <b>Validity Duration:</b> <b>{days_val} Days</b>\n"
                        f"⏳ <b>Expires On:</b> <b>{exp_date}</b>\n\n"
                        "🚀 Enjoy full access to Ghidra Reverse Engineering Engine!\n"
                        "👥 <b>Support Admins:</b> @Ghostofhackers | @R3V_X"
                    ),
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid days number! Please enter a valid number of days (e.g. 30):")
            ADMIN_STATE[user_id] = "AWAITING_SETLIMIT_DAYS"



async def cmd_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    db.data["free_mode"] = True
    db.save()
    await update.message.reply_text("✅ <b>Bot is now in FREE mode!</b>\nAll users can now use the bot without needing approval.", parse_mode="HTML")

async def cmd_unfree(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        return
    db.data["free_mode"] = False
    db.save()
    await update.message.reply_text("❌ <b>Bot is NO LONGER in free mode.</b>\nNew users will need to request approval again. Previously approved users will continue working fine.", parse_mode="HTML")

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.add_approved(target_id)
        PENDING_REQUESTS.discard(target_id)
        
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been approved.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🎉 <b>Access Approved!</b>\nYour request for bot access has been approved by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_APPROVE"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to approve:", parse_mode=constants.ParseMode.HTML)


async def cmd_unapprove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"❌ User <code>{target_id}</code> has been unapproved/revoked.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🔒 <b>Access Revoked</b>\nYour access to the bot has been revoked by the Admin.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_UNAPPROVE"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to unapprove:", parse_mode=constants.ParseMode.HTML)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.ban(target_id)
        db.remove_approved(target_id)
        
        await update.message.reply_text(f"🚫 User <code>{target_id}</code> has been banned.", parse_mode=constants.ParseMode.HTML)
        try:
            await context.bot.send_message(chat_id=int(target_id), text="🚫 <b>Account Banned</b>\nYou have been banned from using this bot.", parse_mode=constants.ParseMode.HTML)
        except Exception:
            pass
    else:
        ADMIN_STATE[uid] = "AWAITING_BAN"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to ban:", parse_mode=constants.ParseMode.HTML)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        target_id = context.args[0].strip()
        db.unban(target_id)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been unbanned.", parse_mode=constants.ParseMode.HTML)
    else:
        ADMIN_STATE[uid] = "AWAITING_UNBAN"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to unban:", parse_mode=constants.ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    if context.args:
        broadcast_msg = update.message.text.split(None, 1)[1]
        target_users = set(db.data["approved"] + list(db.data['daily_usage'].keys()))
        sent, failed = 0, 0
        status_msg = await update.message.reply_text("📢 Broadcasting message...")
        for tu in target_users:
            try:
                await context.bot.send_message(
                    chat_id=int(tu),
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
    else:
        ADMIN_STATE[uid] = "AWAITING_BROADCAST"
        await update.message.reply_text("📢 Please send the <b>Broadcast message text</b> you want to send to all users:", parse_mode=constants.ParseMode.HTML)


async def cmd_setlimit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if uid not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return

    if len(context.args) >= 3:
        target_id = context.args[0].strip()
        try:
            daily_limit = int(context.args[1].strip())
            days_val = int(context.args[2].strip())
            exp_date = (date.today() + timedelta(days=days_val)).isoformat()

            db.set_sub(target_id, exp_date, daily_limit)
            db.add_approved(target_id)

            await update.message.reply_text(
                f"✅ <b>Custom Limit Set Successfully!</b>\n\n"
                f"👤 <b>User ID:</b> <code>{target_id}</code>\n"
                f"📊 <b>Daily Quota:</b> {daily_limit} files/day\n"
                f"📅 <b>Validity:</b> {days_val} days\n"
                f"⏳ <b>Expires On:</b> {exp_date}",
                parse_mode=constants.ParseMode.HTML,
            )
            try:
                await context.bot.send_message(
                    chat_id=int(target_id),
                    text=(
                        "🎉 <b>YOUR SUBSCRIPTION HAS BEEN UPDATED!</b>\n"
                        "═══════════════════════════════════\n"
                        f"🆔 <b>User ID:</b> <code>{target_id}</code>\n"
                        f"📊 <b>Daily Quota:</b> <b>{daily_limit} files / day</b>\n"
                        f"📅 <b>Validity Duration:</b> <b>{days_val} Days</b>\n"
                        f"⏳ <b>Expires On:</b> <b>{exp_date}</b>\n\n"
                        "🚀 Enjoy full access to Ghidra Reverse Engineering Engine!\n"
                        "👥 <b>Support Admins:</b> @Ghostofhackers | @R3V_X"
                    ),
                    parse_mode=constants.ParseMode.HTML,
                )
            except Exception:
                pass
        except ValueError:
            await update.message.reply_text("❌ Invalid parameters! Usage: /setlimit <user_id> <daily_limit> <days>")
    else:
        ADMIN_STATE[uid] = "AWAITING_SETLIMIT_USERID"
        await update.message.reply_text("📝 Please send the <b>User ID</b> you want to set custom limits for:", parse_mode=constants.ParseMode.HTML)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) not in ADMIN_IDS:
        await update.message.reply_text("❌ Only Admins can use this command.")
        return
    now = time.time()
    active_now = len([t for t in active_jobs_timestamps if now - t < 600])
    if GITHUB_TOKEN and GITHUB_REPO:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
                r1 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=in_progress", headers=headers)
                r2 = await client.get(f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=queued", headers=headers)
                if r1.status_code == 200 and r2.status_code == 200:
                    active_now = r1.json().get("total_count", 0) + r2.json().get("total_count", 0)
        except Exception:
            pass
    today_iso = date.today().isoformat()
    today_files = sum(rec["count"] for rec in db.data['daily_usage'].values() if rec.get("date") == today_iso)
    stats_text = (
        "📊 <b>ADMIN SYSTEM STATS</b>\n"
        "═══════════════════════\n"
        f"👥 <b>Approved Users:</b> {len(db.data["approved"])}\n"
        f"🚫 <b>Banned Users:</b> {len(db.data["banned"])}\n"
        f"⭐ <b>Custom Subscriptions:</b> {len(db.data["subscriptions"])}\n"
        f"📅 <b>Total Files Processed Today:</b> {today_files}\n"
        f"⚙️ <b>Active Cloud Jobs:</b> {active_now} / {MAX_CONCURRENT_JOBS}\n"
        f"⏳ <b>Queued Jobs:</b> {job_queue.qsize()}\n\n"
        "⚡ <i>Powered By @Ghostofhackers & @R3V_X</i>"
    )
    await update.message.reply_text(stats_text, parse_mode=constants.ParseMode.HTML)


async def subscription_checker_loop(app: Application):
    while True:
        try:
            today = date.today()
            changed = False
            for uid, sub in list(db.data["subscriptions"].items()):
                try:
                    exp_date = date.fromisoformat(sub["expires_at"])
                    days_left = (exp_date - today).days

                    # 5 Days Warning
                    if 1 < days_left <= 5 and not sub.get("warned_5"):
                        sub["warned_5"] = True
                        changed = True
                        msg_text = (
                            "⚠️ <b>SUBSCRIPTION EXPIRY WARNING</b>\n"
                            "═══════════════════════════════════\n"
                            f"Your bot subscription will expire in <b>{days_left} days</b> (Expires: <code>{exp_date}</code>).\n\n"
                            "⚠️ Please contact an Admin to renew your subscription so you don't lose access!\n"
                            "👥 <b>Admins:</b> @Ghostofhackers | @R3V_X"
                        )
                        try:
                            await app.bot.send_message(
                                chat_id=int(uid),
                                text=msg_text,
                                parse_mode=constants.ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("👤 Contact Admin to Renew", url="https://t.me/Ghostofhackers")]
                                ])
                            )
                        except Exception as e:
                            log.warning("Failed to send 5-day warning to %s: %s", uid, e)

                    # 1 Day Warning (24h before expiry)
                    elif 0 <= days_left <= 1 and not sub.get("warned_1"):
                        sub["warned_1"] = True
                        changed = True
                        msg_text = (
                            "🚨 <b>URGENT: SUBSCRIPTION EXPIRING TOMORROW!</b>\n"
                            "═══════════════════════════════════\n"
                            f"Your bot subscription will expire in <b>{max(1, days_left)} day</b> (Expires: <code>{exp_date}</code>).\n\n"
                            "⚠️ Contact Admin to renew immediately so you don't lose access!\n"
                            "👥 <b>Admins:</b> @Ghostofhackers | @R3V_X"
                        )
                        try:
                            await app.bot.send_message(
                                chat_id=int(uid),
                                text=msg_text,
                                parse_mode=constants.ParseMode.HTML,
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🔄 Renew Subscription", url="https://t.me/Ghostofhackers")]
                                ])
                            )
                        except Exception as e:
                            log.warning("Failed to send 1-day warning to %s: %s", uid, e)

                except Exception as e:
                    log.warning("Error checking subscription for %s: %s", uid, e)

            if changed:
                db.save()
        except Exception as e:
            log.exception("Error in subscription_checker_loop", exc_info=e)

        await asyncio.sleep(21600)  # Check every 6 hours


async def weekly_analytics_loop(app: Application):
    while True:
        await asyncio.sleep(604800)  # Every 7 days
        try:
            today = date.today()
            today_iso = today.isoformat()
            today_files = sum(rec["count"] for rec in db.data['daily_usage'].values() if rec.get("date") == today_iso)
            report_text = (
                "📈 <b>AUTOMATED WEEKLY ADMIN ANALYTICS REPORT</b>\n"
                "═══════════════════════════════════\n"
                f"👥 <b>Total Approved Users:</b> {len(db.data["approved"])}\n"
                f"⭐ <b>Custom Subscribers:</b> {len(db.data["subscriptions"])}\n"
                f"🚫 <b>Banned Users:</b> {len(db.data["banned"])}\n"
                f"📅 <b>Today's Files Processed:</b> {today_files}\n"
                "⚙️ <b>Server Health:</b> 100% Operational 🔥\n\n"
                "⚡ <i>Powered By @Ghostofhackers & @R3V_X</i>"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await app.bot.send_message(
                        chat_id=int(admin_id),
                        text=report_text,
                        parse_mode=constants.ParseMode.HTML,
                    )
                except Exception as e:
                    log.warning("Failed to send weekly report to %s: %s", admin_id, e)
        except Exception as e:
            log.exception("Error in weekly_analytics_loop", exc_info=e)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error", exc_info=context.error)



async def cleanup_workflows_loop(app: Application):
    while True:
        try:
            if GITHUB_TOKEN and GITHUB_REPO:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    headers = {
                        "Authorization": f"Bearer {GITHUB_TOKEN}",
                        "Accept": "application/vnd.github+json"
                    }
                    r = await client.get(
                        f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs",
                        headers=headers
                    )
                    if r.status_code == 200:
                        runs = r.json().get("workflow_runs", [])
                        for run in runs:
                            if run.get("status") == "completed":
                                await client.delete(
                                    f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs/{run['id']}",
                                    headers=headers
                                )
        except Exception as e:
            pass # Silent failure to avoid spamming logs if there's an issue
        await asyncio.sleep(60)  # Check every 60 seconds


async def post_init(app: Application):
    asyncio.create_task(queue_worker_loop())
    asyncio.create_task(subscription_checker_loop(app))
    asyncio.create_task(weekly_analytics_loop(app))
    asyncio.create_task(cleanup_workflows_loop(app))


def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN env set nahi hai!")
        sys.exit(1)

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    
    app.add_handler(CommandHandler("approved_users", cmd_list_users))
    app.add_handler(CommandHandler("unapproved_users", cmd_list_users))
    app.add_handler(CommandHandler("ban_users", cmd_list_users))
    app.add_handler(CommandHandler("banned_users", cmd_list_users))
    app.add_handler(CommandHandler("premium_users", cmd_list_users))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("free", cmd_free))
    app.add_handler(CommandHandler("unfree", cmd_unfree))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("unapprove", cmd_unapprove))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("setlimit", cmd_setlimit))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_text_message))
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
        
        # Start a dummy HTTP server to pass Railway healthchecks
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class HealthCheckHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            def log_message(self, format, *args):
                pass
        
        def start_dummy_server():
            try:
                server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
                log.info(f"Dummy HTTP server started on port {PORT} for health checks")
                server.serve_forever()
            except Exception as e:
                log.error(f"Failed to start dummy HTTP server: {e}")
                
        threading.Thread(target=start_dummy_server, daemon=True).start()
        
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
