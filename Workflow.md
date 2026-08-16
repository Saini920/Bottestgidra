# 🔄 Full Workflow — Web App (RE Tools + Telegram Storage)

> Complete workflow documentation for turning the **Ghidra Telegram Bot** into a
> fully working **web application** with:
> - **MTProto login** (API ID + API HASH + phone → OTP → **2FA password screen**)
> - **RE Tools website** (all 13 decompilation engines, real-time progress)
> - **Storage website** (files stored in the user's own Telegram "Saved Messages")
>
> The web app works **with or without** the Telegram bot being hosted.
> The bot is optional — the web app is its own control plane.

---

## 1. What Stays the Same vs What We Build

| Layer | Existing (bot) | Web app |
|---|---|---|
| **UI** | Telegram chat | Next.js/React browser app |
| **Control plane** | `bot.py` (Telegram handlers) | FastAPI backend (HTTP + WebSocket) |
| **Login** | Bot token (no login) | MTProto user login: phone → OTP → 2FA password |
| **Compute (13 engines)** | GitHub Actions workflows | **Unchanged** — same dispatch, same workers |
| **File transfer** | Bot API + bot MTProto sessions | User's own MTProto session (no 50 MB limit) |
| **Database** | `database.json` on git `data` branch | Web backend DB (SQLite/Postgres) |
| **Real-time progress** | `editMessageText` on Telegram | Worker → webhook → WebSocket → browser |

**Key insight:** the heavy compute (Ghidra, JADX, Apktool, Smali, DEX compile,
NDK C/C++, APK build/sign, PDF→TXT) runs on **GitHub Actions runners** and is
fully decoupled from the Telegram UI. Replacing the UI layer leaves all
workflows + worker scripts untouched — that is the "all tools work properly"
guarantee.

---

## 2. Architecture

### 2a. Bot-less mode (recommended — bot optional)

```
┌─ Browser (Next.js/React, one combined app) ──────┐
│  Login wizard · Dashboard · Storage · RE Tools    │
└──────────────┬──────────────────┬─────────────────┘
               │ HTTPS            │ WebSocket (live progress)
        ┌──────▼───────┐    ┌─────▼───────────────────┐
        │ FastAPI API  │    │ /ws/progress/{job_id}   │
        └──────┬───────┘    └─────────────────────────┘
               │ Pyrogram (MTProto) — per-user login
               │ API ID + API HASH + phone → OTP → 2FA
        ┌──────▼──────────────────────────────────────┐
        │ Telegram — user's own Saved Messages        │  ← storage layer
        └──────┬──────────────────────────────────────┘
               │ repository_dispatch (unchanged)
        ┌──────▼──────────────────────────────────────┐
        │ GitHub Actions — all 12 workflows, all 13   │  ← compute layer
        │ engines, workers untouched (+ status webhook)│
        └─────────────────────────────────────────────┘
```

### 2b. Hybrid mode (bot + web together)

The existing `bot.py` keeps serving Telegram users. Both interfaces share:
- the same GitHub Actions dispatch + workers,
- the same quota/premium logic (ported to the web backend),
- separate databases (bot's `database.json` vs web DB).

---

## 3. Login Workflow (MTProto + 2FA)

```
User opens website
        │
        ▼
┌─ Screen 1: Phone number ─┐   POST /auth/send-code
│  +91 98XXXXXXXX          │   Pyrogram: send_code(phone)
└──────────┬───────────────┘   → code sent to user's Telegram app
           ▼
┌─ Screen 2: Enter OTP ────┐   POST /auth/verify-code
│  5-digit code             │   Pyrogram: sign_in(phone, code)
└──────────┬───────────────┘
           │
           ├── 2FA enabled? ──► Pyrogram raises SessionPasswordNeeded
           │                     │
           │                     ▼
           │            ┌─ Screen 3: 2FA Password ─┐  POST /auth/check-password
           │            │  (password screen)       │  Pyrogram: check_password(pw)
           │            └──────────┬───────────────┘
           │                       ▼
           │                  ✅ Logged in
           │
           └── 2FA disabled? ──► ✅ Logged in directly

✅ After login:
   - Session string exported + encrypted at rest (per user)
   - httpOnly cookie / JWT issued to browser
   - Redirect → Dashboard
```

### Edge cases handled
- Wrong OTP → `PHONE_CODE_EXPIRED` → resend code
- Wrong 2FA password → error shown, screen repeats
- Account already logged in elsewhere → `SessionPasswordNeeded` or direct sign-in handled
- `FloodWait` → backoff + retry (pattern already used across the codebase)
- Logout → session revoked in Telegram + deleted from DB

---

## 4. Storage Workflow (user's Telegram = cloud storage)

**Where files live:** the user's own Telegram account → **Saved Messages**
(self-chat, `"me"`). The web server stores **only metadata** (message_id,
filename, size, date) — never the file data.

```
Dashboard → Storage tab
        │
        ├── UPLOAD:
        │     Browser ──► POST /storage/upload (multipart)
        │       FastAPI streams ──► Pyrogram send_document("me", file, progress=cb)
        │       WebSocket: "▰▰▰▰▰▱▱▱ 62%" live
        │       ✅ File card added to list (metadata saved in DB)
        │
        ├── LIST:  Pyrogram get_history("me") → filter documents
        │
        ├── DOWNLOAD:
        │     Click ⬇️ ──► GET /storage/download/{msg_id}
        │     Pyrogram download_media (live %) ──► streamed to browser
        │     (File stays in Saved Messages)
        │
        └── DELETE:
              🗑️ ──► delete_messages("me", [msg_id]) + DB metadata removed
```

### Limits (Telegram cloud storage)
| | Free | Premium |
|---|---|---|
| Total storage | 2 GB | 4 GB |
| Max single file | 2 GB | 4 GB |

**Privacy:** files belong to the user's own account. The server never holds a
copy — it only streams during upload/download.

---

## 5. RE Tools Workflow (13 engines)

```
Dashboard → RE Tools tab
        │
        ▼
User uploads a file (.exe .dll .so .elf .apk .zip .dex .pdf .smali .java .c/.cpp ...)
        │
        ▼
Engine chooser (per extension, same 13 engines + free/premium gating)
        │
        ▼
POST /api/jobs { file, engine, user }
        │
        ├─ FastAPI stores file in temp storage, creates signed URL (1 hr, token)
        │
        ▼
repository_dispatch to GitHub Actions   ← identical to bot.py's dispatch
   (client_payload: chat_id, filename, file_url, job_id, status_webhook, ...)
        │
        ▼
GitHub Actions runner (ubuntu-latest, toolchain cached: Ghidra/JADX/dex2jar/CFR)
        │
        ▼
Worker: downloads file (signed URL via existing download_url()) → runs tool
        │
        ├─ Progress: worker POSTs JSON to /status webhook (token-protected)
        │     └─► FastAPI ──► WebSocket /ws/progress/{job_id} ──► browser
        │          "🔧 Analyzing... ▰▰▰▱▱ 45% ⏱ 2m 10s"
        │
        ▼
Result ZIP (decompiled.c / Java / Smali / APK / JAR / TXT + info.txt)
        │
        ▼
Worker POSTs result ZIP back to web backend (multipart) → temp storage
        │
        ▼
Browser: download button → user gets the ZIP
        │
        ▼
🧹 Cleanup job: upload + result ZIP auto-deleted (24 h or right after download)
        │
        ▼
[🛑 Stop Processing] → GitHub Actions API cancels the run (same as bot)
```

### 13 Engines (unchanged from the bot)
| # | Engine | Input → Output |
|---|---|---|
| 1 | ⚙️ Ghidra | any binary/ZIP → `*.c` + `*_info.txt` |
| 2 | ☕ JADX | APK/DEX → Java source |
| 3 | 🧬 dex2jar + CFR | APK/DEX → JAR + Java (JADX fallback) |
| 4 | 📱 Apktool (decompile) | APK → XML + Smali ⭐Premium |
| 5 | 🔨 Apktool build | decompiled ZIP → signed APK ⭐Premium |
| 6 | 🧩 Smali decode | .dex → Smali (full or `com/`) |
| 7 | 🛠️ Smali → DEX | .smali → classes.dex ⭐Premium |
| 8 | ☕ Java → DEX | .java/.jar → classes.dex ⭐Premium |
| 9 | ⚙️ C/C++ → .so | .c/.cpp → Android ARM64 .so ⭐Premium |
| 10 | 📦 APK build (source) | source ZIP → signed APK ⭐Premium |
| 11 | 🔏 APK sign | APK → re-signed APK ⭐Premium |
| 12 | 📄 PDF → TXT | .pdf → text (+OCR for scanned) |
| 13 | 📦 Batch Ghidra | ZIP of binaries → per-file .c |

---

## 6. File Storage & Lifecycle — Where Files Live

| Scenario | Where the file is | Lifetime |
|---|---|---|
| **Storage tab upload** | Telegram Saved Messages (user's account) | Until user deletes it |
| **Storage tab metadata** | Web DB (message_id, name, size, date) | Until file deleted |
| **Storage tab download** | Streamed through server (no copy kept) | Momentary |
| **RE Tools upload** | Web server temp disk | 1–2 hours max |
| **RE Tools processing** | GitHub Actions runner `/tmp` | Until job finishes |
| **RE Tools result ZIP** | Web server temp disk | 24 h or until downloaded |
| **RE Tools final** | User's browser (downloaded ZIP) | — |
| **Cleanup** | Everything temp auto-deleted | Automatic 🧹 |

**Storage tab** = permanent files in the user's Telegram.
**RE Tools** = temporary files, deleted after the job — by design (sensitive code,
quota savings).

---

## 7. Real-Time Design

```
Browser ⇄ WebSocket ⇄ FastAPI ⇄ GitHub Actions worker
                                  ↑
               worker POSTs status to /status webhook with token
```

- WebSocket channel per job: `/ws/progress/{job_id}`
- Workers already stream progress (`edit()` / `notify_app()`); add a
  `report_status()` helper that POSTs JSON to a `PAYLOAD_STATUS_URL` env var
  (~15 lines per worker, or one shared `worker_common.py`)
- Telegram `editMessageText` kept as fallback in hybrid mode

---

## 8. Build & Deploy Workflow (Phases)

### Phase 1 — Foundation + Storage (proves login/2FA/real-time)
1. Scaffold `web/backend` (FastAPI) + `web/frontend` (Next.js/React)
2. Login API: `POST /auth/send-code` → `POST /auth/verify-code` →
   `POST /auth/check-password` (2FA) → encrypted session persistence + logout revoke
3. Login wizard UI: phone → OTP → 2FA password screen
4. Storage API: upload/list/download/delete on Saved Messages +
   WebSocket `/ws/progress/{job_id}`
5. Storage UI: file manager with live progress bars
6. Deploy backend + frontend; test end-to-end with a real 2FA-enabled account

### Phase 2 — RE Tools
7. Add `report_status()` + `PAYLOAD_STATUS_URL` webhook to workers (keep Telegram fallback)
8. Job API: file upload → signed URL → `repository_dispatch` (existing engines)
9. Job UI: engine chooser, live progress, result download, stop button
10. Port quota / premium / admin logic from `bot.py` into web DB

### Phase 3 — Hardening
11. Login rate limiting, session encryption, error pages
12. Production deploy (single combined app)
13. Load test + coexistence test with the Telegram bot (hybrid mode)

---

## 9. Environment Variables (Web Backend)

| Variable | Required | Purpose |
|---|---|---|
| `API_ID` | ✅ | MTProto login + storage (Pyrogram) |
| `API_HASH` | ✅ | MTProto login + storage (Pyrogram) |
| `GITHUB_TOKEN` | ✅ | GitHub Actions dispatch / cancel (repo scope) |
| `GITHUB_REPO` | ✅ | `owner/repo` containing workflows + workers |
| `DB_URL` | ✅ | SQLite/Postgres connection |
| `SESSION_SECRET` | ✅ | Encrypt stored session strings |
| `WEBHOOK_TOKEN` | ✅ | Token workers use to POST status/results |
| `PUBLIC_URL` | ✅ | Signed URLs + webhook reachability |
| `TELEGRAM_BOT_TOKEN` | optional | Only for hybrid mode (bot kept running) |

No `TELEGRAM_BOT_TOKEN` needed in bot-less mode.

---

## 10. Constraints, Limits & Risks

| Item | Detail | Mitigation |
|---|---|---|
| Telegram storage limits | Free 2 GB total / 2 GB per file; Premium 4 GB | Show usage bar in UI |
| Login rate limits | Shared server IP → `FloodWait` on logins | Backoff/retry, cap concurrency |
| GitHub Actions quota | ~2000 min/month free (public repo); each job burns minutes | Cache toolchains, cleanup old runs, `/active` monitoring |
| Userbot ToS risk | User accounts used for automation | Keep behavior human-like, rate-limit, warn users |
| Session strings | Sensitive — user's Telegram access | Encrypt at rest, revoke on logout, never log |
| File size | Bot API 50 MB limit avoided via MTProto + signed URLs | Use user sessions + direct HTTP streaming |

---

## 11. TL;DR

1. **Login:** phone → OTP → 2FA password screen (MTProto, per-user session)
2. **Storage:** files live in the user's own Telegram Saved Messages —
   server keeps only metadata; 2 GB free / 4 GB Premium
3. **RE Tools:** all 13 engines run on GitHub Actions (unchanged) —
   file goes temp → signed URL → worker → result ZIP back → auto-delete
4. **Real-time:** WebSocket everywhere — no page reloads
5. **Bot optional:** web app is its own control plane; bot can keep running
   alongside (hybrid) or be shut down
6. **Build order:** Phase 1 storage (login+2FA+realtime) → Phase 2 RE tools →
   Phase 3 hardening
