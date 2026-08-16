# 🔬 Project Analysis — Ghidra Decompiler Telegram Bot

> Full technical analysis of the `ghidra-telegram-bot-master` project
> (a.k.a. **"Ghidra Decompiler Bot"** / **@R3V_X's reverse-engineering bot**)

---

## 1. Project Overview

This is a **Telegram bot that performs reverse-engineering / decompilation of binary files** entirely in the cloud.

- A user sends a binary file (`.exe`, `.dll`, `.so`, `.elf`, `.apk`, `.zip`, `.dex`, `.pdf`, source code, etc.) to the bot on Telegram.
- The bot analyzes it using **Ghidra (NSA's reverse-engineering framework)** and a suite of **Android/Java reverse-engineering tools**, then sends back the decompiled output (C code, Java source, Smali code, JAR, rebuilt/signed APK, text, etc.).
- It is a **commercial-style bot** with a Free/Premium subscription model (₹99), admin panel, user approval workflow, daily quotas, and file-size limits.

**Key identity markers found in the code:**
| Item | Value |
|---|---|
| Bot brand / channel | `@R3V_X` (admin), `@allinformation0173` (force-join channel) |
| GitHub repo used as compute backend | `Saini920/Bottestgidra` (default) |
| Script version string | `v4-gh` |
| Admin Telegram IDs (hardcoded) | `6684870256`, `7251749429` |

---

## 2. Tech Stack

| Layer | Technology |
|---|---|
| Bot framework | `python-telegram-bot` v21+ (async) |
| HTTP client | `httpx` (async) |
| MTProto (large file transfer) | `Pyrogram` + `tgcrypto` |
| Secret encryption | `PyNaCl` (GitHub Actions secrets) |
| Language | Python 3.12 (bot), Python 3 (workers), Java (Ghidra scripts + JVM tools) |
| Bot host | Railway.com (Docker, `python:3.12-slim`) |
| Compute / workers | **GitHub Actions runners** (`ubuntu-latest`, dispatched via `repository_dispatch`) |
| RE engine | **Ghidra 11.3.2** (`analyzeHeadless`) |
| Android tools | JADX, dex2jar, CFR, Apktool, baksmali/smali, D8/R8, Android SDK build-tools, NDK |
| Doc tools | poppler-utils (`pdftotext`, `pdftoppm`), Tesseract OCR |
| Data store | JSON file (`database.json`) stored on a dedicated git branch (`data`) of the GitHub repo |
| Notification | `ntfy.sh` (per-job push topics) |

---

## 3. Architecture & How It Works (End-to-End Flow)

The system is split into **two planes**:

```
┌──────────────────────────────┐          ┌──────────────────────────────────────┐
│  CONTROL PLANE (Railway)     │          │  WORKER PLANE (GitHub Actions)        │
│  bot.py + database.py        │          │  worker_*.py + .github/workflows/*.yml │
│                              │          │                                      │
│  • Telegram updates          │  POST     │  • Downloads file from Telegram      │
│  • Users / admins / quotas   │─────────►│  • Runs the chosen RE tool            │
│  • Engine chooser buttons    │ dispatch  │  • Uploads result ZIP back to chat   │
│  • Triggers GitHub workflows │  API      │                                      │
└──────────────────────────────┘          └──────────────────────────────────────┘
```

### Step-by-step flow

1. **User sends a file** to the bot (or uses a command).
2. **`bot.py` validates the request:**
   - `check_force_join()` → user must be a member of the force-join channels.
   - `is_allowed()` → user must be approved / admin / free-mode / premium.
   - `check_daily_limit()` → daily file quota (30/day free, custom for premium, unlimited for admins).
   - File size limits per file type (see §9).
3. **Engine chooser** — `handle_file()` inspects the file extension and shows inline buttons:
   - `.apk` → JADX / dex2jar / Apktool / Sign APK
   - `.zip` → Ghidra / JADX / dex2jar / Smali decode / DEX compile / C/C++ compile / APK build / Apktool build / PDF→TXT
   - `.dex` → JADX / dex2jar / Smali
   - `.smali`, `.java`, `.jar/.class`, `.c/.cpp` → compile options
   - `.pdf` → PDF→TXT
   - anything else → default Ghidra engine
4. **Queueing / concurrency control** — `enqueue_or_dispatch()`:
   - Max **4 concurrent jobs** (sliding 10-minute window).
   - **Priority fast-lane**: admins & premium subscribers skip the queue.
   - Others wait in an `asyncio.Queue` with a "Stop Processing" cancel button.
5. **Dispatch to GitHub** — `trigger_github()` POSTs a `repository_dispatch` event to the GitHub repo with a `client_payload` containing: `chat_id`, `message_id`, `original_message_id`, `filename`, `bot_token`, `is_admin`, `is_premium`, `file_id`, `tg_file_path`, `min_sdk`.
6. **GitHub Actions runner starts** — the matching workflow YAML (per engine) boots `ubuntu-latest`, caches toolchains (Ghidra/JADX/dex2jar/CFR), restores Telegram MTProto sessions, and runs the engine's worker script.
7. **Worker downloads the file** (in priority order):
   - HTTP Bot API download via `tg_file_path` (fast path),
   - **MTProto fallback** via Pyrogram (`download_file.py`) for files > ~50 MB or when HTTP fails (with FloodWait retries + 45 s stall watchdog),
   - direct URL download (`download_url()` — supports plain links, **Google Drive** links, and **MediaFire** links).
8. **Worker processes the file** with the chosen tool (Ghidra, JADX, baksmali, apksigner, etc.), streaming live progress to the user via `editMessageText` (animated `▰▱` progress bar + elapsed time).
9. **Worker uploads the result** — if output ≤ 50 MB via Bot API HTTP (`sendDocument`); if larger, via **MTProto** (`upload_file.py`, Pyrogram).
10. **Cancellation** — the "🛑 Stop Processing" button calls GitHub Actions API to cancel the running workflow.

### Background loops in `bot.py` (started in `post_init`)

| Loop | Interval | Purpose |
|---|---|---|
| `queue_worker_loop` | continuous | Drains the job queue respecting concurrency |
| `subscription_checker_loop` | 6 h | Sends 5-day & 1-day expiry warnings to premium users |
| `weekly_analytics_loop` | 7 days | Sends weekly stats report to admins |
| `cleanup_workflows_loop` | 60 s | Deletes completed GitHub runs older than 5 min (saves quota) |
| `track_runs_loop` | 20 s | Syncs GitHub run status → `ACTIVE_JOBS` for `/active` |

### HTTP side-server (polling mode only)

`bot.py` starts a tiny `ThreadingHTTPServer` on `PORT` that:
- answers `GET /` → `OK` (Railway health check),
- accepts `POST /internal/count` (token-protected with the bot token) — workers POST extra file counts for batch ZIPs so quotas are billed correctly.

---

## 4. Component Breakdown (File-by-File)

### Core (bot side)
| File | Role |
|---|---|
| `bot.py` (~2225 lines) | Main Telegram bot: all commands, callbacks, engine chooser, quota logic, GitHub dispatch, background loops, HTTP health server |
| `database.py` | `RepoDB` class — reads/writes `database.json` on a dedicated `data` git branch (so frequent saves never redeploy Railway, which watches `main`) |
| `requirements.txt` | `python-telegram-bot>=21,<22`, `httpx>=0.27`, `PyNaCl>=1.5.0` |
| `Dockerfile` | `python:3.12-slim` image, runs `python bot.py` on port 8080 |
| `.env` loader | `bot.py` auto-loads a `.env` file if present (dev convenience) |

### Worker scripts (run on GitHub Actions runners)
| File | Engine | Toolchain used |
|---|---|---|
| `worker.py` | **Ghidra** decompile (+ batch ZIP) | `analyzeHeadless` + custom Java scripts |
| `worker_jadx.py` | **JADX** (APK/DEX → Java) | `jadx` CLI |
| `worker_dex2jar.py` | **dex2jar + CFR** (→ JAR + Java) | `dex2jar`, `cfr.jar`, JADX fallback |
| `worker_apktool.py` | **Apktool decompile** (XML/Smali) | `apktool.jar d` |
| `worker_apktool_build.py` | **Apktool build** (ZIP → signed APK) | `apktool.jar b`, `zipalign`, `apksigner` |
| `worker_smali.py` | **Smali decode** (DEX → Smali, full / com/ extract) | `baksmali.jar` |
| `worker_dex_compile.py` | **DEX compile** (Smali → .dex, Java → .dex) | `smali.jar assemble`, `javac`, `r8.jar` (D8) |
| `worker_cc_compile.py` | **C/C++ → Android .so** | Android NDK `clang`/`clang++` (aarch64) |
| `worker_apk_build.py` | **APK build from source** (full Android build) | `aapt2`, `javac`, `kotlinc`, D8, NDK multi-ABI, `zipalign`, `apksigner` |
| `worker_apk_sign.py` | **APK re-sign** (v1+v2, Android 5–16) | `zipalign`, `apksigner`, `keytool` |
| `worker_pdf_txt.py` | **PDF → TXT** (+ OCR for scanned PDFs) | `pdftotext`, `pdftoppm`, `tesseract` |
| `download_file.py` | MTProto download helper (Pyrogram) | session pooling, FloodWait handling, stall watchdog |
| `upload_file.py` | MTProto upload helper (Pyrogram) | same resilience pattern |

### Ghidra scripts (`ghidra_scripts/`)
| File | Role |
|---|---|
| `DecompileAll.java` | Headless Ghidra script: writes `decompiled.c` (all functions, decompiler interface recycled every 1500 functions to avoid OOM) + `info.txt` (file info, language, compiler, up to 20k strings, up to 20k symbols). Emits `DECOMP_PROGRESS n/total` for the live progress bar |
| `DisableCallFixup.java` | Disables `CallFixupAnalyzer` — used as a crash-retry fallback when normal analysis fails |

### GitHub Actions workflows (`.github/workflows/`)
| Workflow | `repository_dispatch` type | Run name prefix |
|---|---|---|
| `decompile_v2.yml` | `decompile-job` | `job-{chat}-{msg}` |
| `jadx_v2.yml` | `decompile-jadx` | `jadx-` |
| `dex2jar.yml` | `decompile-dex2jar` | `dex2jar-` |
| `apktool_v2.yml` | `decompile-apktool` | `apktool-` |
| `apktool_build.yml` | `compile-apktool` | `build-` |
| `smali.yml` | `decompile-smali` / `decompile-smali-extract` | `smali-` / `smaliextract-` |
| `dex_compile_v2.yml` | `dex-compile-smali` / `dex-compile-java` | `dexcompile-*` |
| `cc_compile_v2.yml` | `cc-compile` | `cccompile-` |
| `apk_build_v2.yml` | `apk-source-build` | `apkbuild-` |
| `apk_sign_v2.yml` | `apk-sign` | `apksign-` |
| `pdf_txt.yml` | `pdf-to-txt` | `pdftxt-` |
| `release_apk.yml` | — (manual/release) | — |

Each workflow: checks out repo → sets up Java 21 (Temurin) → restores toolchain from `actions/cache` (Ghidra 11.3.2 key, JADX 1.5.6, dex2jar+CFR v2, TG sessions) → restores Telegram MTProto sessions from the `TG_SESSIONS_B64` secret → runs the worker with `PAYLOAD_*` env vars → `timeout-minutes: 1440`.

### Legacy / helper files
| File | Role |
|---|---|
| `refactor.py`, `refactor2.py`, `refactor3.py` | **One-time migration scripts** (leftovers) — they previously rewrote `bot.py` from in-memory sets to `RepoDB` and added force-join logic. Not part of runtime |
| `user_subscriptions.json` | **Legacy leftover** subscription file (superseded by `database.json` on the `data` branch) |
| `database.json` | Sample/backup copy of the persisted DB (approved users, names, daily usage) |
| `.gitignore` | Ignores env files, sessions, logs, tmp dirs, zips |

---

## 5. All Tools Used (External Programs / Libraries)

### Reverse-engineering & decompilation
| Tool | Version (pinned) | Purpose |
|---|---|---|
| **Ghidra** | 11.3.2 | Headless binary analysis + C decompilation (`analyzeHeadless`) |
| **JADX** | 1.5.6 | APK/DEX → Java source (also used as fallback in dex2jar worker) |
| **dex2jar** | v2.4 | DEX → JAR |
| **CFR** | 0.152 | JAR → Java source |
| **Apktool** | (jar at `/opt/apktool/apktool.jar`) | APK decode (`d`) & build (`b`) |
| **baksmali** | `/opt/baksmali.jar` | DEX → Smali disassembly |
| **smali** | `/opt/smali.jar` | Smali → DEX assembly |

### Android build chain
| Tool | Purpose |
|---|---|
| Android SDK build-tools: **aapt2, apksigner, zipalign, d8** | Resource compile/link, signing, alignment, DEX |
| Android **NDK** clang / clang++ (`aarch64-linux-android24-clang`) | C/C++ → ARM64 `.so` |
| **javac** | Java → `.class` |
| **kotlinc** | Kotlin → `.class` (APK build) |
| **R8/D8** (`r8.jar`) | `.class`/JAR → `classes.dex` |
| **keytool** | Generate debug keystore / inspect custom keystores |

### Document processing
| Tool | Purpose |
|---|---|
| **pdftotext** (poppler) | PDF → text (`-layout`, `-raw`, `-fixed` progressive fallbacks) |
| **pdftoppm** (poppler) | Render scanned PDF pages to JPEG (pre-OCR) |
| **tesseract** | OCR for scanned PDFs — languages: `eng+hin+urd+ben+mar+guj+tam+tel+kan+mal+pan` (PSM 3/6/11) |

### Python libraries
`python-telegram-bot`, `httpx`, `PyNaCl`, `Pyrogram`, `tgcrypto` (runtime), plus stdlib `asyncio`, `zipfile`, `base64`, etc.

### Web services (integrated)
| Service | Use |
|---|---|
| **Telegram Bot API** | All messaging, file download/upload (≤50 MB), progress updates |
| **Telegram MTProto** (Pyrogram) | File transfer > 50 MB (bypasses Bot API limit) |
| **GitHub REST API** | `repository_dispatch` trigger, run cancel/delete/list, encrypted secret management |
| **ntfy.sh** | Push notifications to a companion app (`notify_app`, topic = `JOB_ID`) |
| **Google Drive / MediaFire** | Direct-link downloads resolved by workers' `download_url()` |

---

## 6. Features List

**Core**
- ✅ Binary decompilation: EXE / DLL / SO / ELF / Mach-O / APK / firmware / any binary
- ✅ Output: full C code of every function (`decompiled.c`) + `info.txt` (language, compiler, strings, symbols) as a ZIP
- ✅ 13 selectable processing **engines** (see §7)
- ✅ Smart APK scanner — auto-extracts & decompiles native `.so` files inside APKs/ZIPs (batch mode)
- ✅ Batch ZIP decompilation (multi-binary, capped per tier)
- ✅ Live progress bar (0–100%) with elapsed time on the status message
- ✅ "🛑 Stop Processing" cancellation for queued & running jobs

**File handling**
- ✅ Multiple download paths: Bot API HTTP → MTProto (Pyrogram) → direct URL (Google Drive / MediaFire)
- ✅ Multiple upload paths: Bot API (≤50 MB) → MTProto (>50 MB)
- ✅ Per-extension engine chooser with inline keyboards

**User system**
- ✅ Access approval workflow (request → admin approve/deny)
- ✅ Force-join channels gating
- ✅ Free mode toggle (open bot without approval)
- ✅ Ban / unban
- ✅ Daily quota per user (30 free / custom premium / unlimited admin)
- ✅ `/profile` with remaining quota, subscription info, server load
- ✅ Premium subscription model (₹99) with **custom daily limits & expiry dates**
- ✅ Automated **5-day & 1-day expiry warnings**
- ✅ Weekly automated analytics report to admins
- ✅ `/broadcast` announcements to all users
- ✅ Custom APK signing keys per user (`/setkey` / `/delkey`, stored as encrypted GitHub secrets)

**Admin panel**
- ✅ Approve / unapprove / ban / unban / setlimit (interactive or one-line args)
- ✅ `/stats` (system-wide usage statistics)
- ✅ `/active` (live cloud jobs with per-job stop buttons, recent runs history)
- ✅ `/approved_users` `/unapproved_users` `/ban_users` `/premium_users` (user lists)
- ✅ Automatic cleanup of old completed GitHub runs (quota saving)

**Resilience**
- ✅ Ghidra crash-retry with `DisableCallFixup` on second attempt
- ✅ JADX fallback when dex2jar or CFR crashes
- ✅ Stall detection (CPU-activity watchdog kills hung tools after 30 min)
- ✅ Download/upload retries with FloodWait handling
- ✅ Error log file auto-sent to user on worker crashes
- ✅ `JAVA_MAX_MEM` auto-tuning to available RAM (writes `launch.properties`)

---

## 7. Engines List (13 Engines)

| # | Engine | Input | Output | Access |
|---|---|---|---|---|
| 1 | ⚙️ **Ghidra** | any binary / ZIP of binaries | `*.c` + `*_info.txt` (ZIP) | Free |
| 2 | ☕ **JADX** | APK / DEX / Smali / ZIP | Java source | Free ≤30 MB, Premium ≤100 MB |
| 3 | 🧬 **dex2jar + CFR** | APK / DEX / ZIP | JAR + Java source (JADX fallback) | Free ≤30 MB, Premium ≤100 MB |
| 4 | 📱 **Apktool** (decompile) | APK | XML resources + Smali (ZIP) | ⭐ Premium |
| 5 | 🔨 **Apktool build** | decompiled APK project ZIP | unsigned + signed APK | ⭐ Premium |
| 6 | 🧩 **Smali decode** | .dex / ZIP of .dex | Smali code (full or `com/` extract) | Free (≤3 dex), Premium (≤10 dex) |
| 7 | 🛠️ **Smali → DEX** | .smali / ZIP | `classes.dex` | ⭐ Premium |
| 8 | ☕ **Java → DEX** | .java / .jar / .class / ZIP | `classes.dex` (javac + D8) | ⭐ Premium |
| 9 | ⚙️ **C/C++ → .so** | .c / .cpp / ZIP | Android ARM64 `lib_*.so` (NDK) | ⭐ Premium |
| 10 | 📦 **APK build (source)** | real source ZIP (Java/Kotlin + C/C++) | signed + unsigned APK (multi-ABI) | ⭐ Premium |
| 11 | 🔏 **APK sign** | any APK | re-signed APK (v1+v2, minSdk 5–16) | ⭐ Premium |
| 12 | 📄 **PDF → TXT** | .pdf / ZIP of PDFs | plain text (OCR for scanned) | Free ≤30 MB, Premium ≤300 MB |
| 13 | 📦 **Batch Ghidra** | APK/ZIP with binaries | per-file `.c` + `info.txt` | Free (≤5 binaries) |

---

## 8. Bot Commands

### User commands
| Command | Description |
|---|---|
| `/start` | Welcome guide, features & limits, buy premium button |
| `/help` | Full command reference & limits |
| `/profile` | Your ID, plan, daily quota remaining, server load |
| `/myid` | Show your Telegram user ID |
| `/setkey` | Set a custom APK signing key (`release.jks`) — 2-step flow (file + storepass) |
| `/delkey` | Delete your custom signing key |

### Admin commands (hardcoded admin IDs only)
| Command | Description |
|---|---|
| `/approve <id>` | Approve user access (interactive or inline arg) |
| `/unapprove <id>` | Revoke user access |
| `/ban <id>` / `/unban <id>` | Ban / unban users |
| `/free` / `/unfree` | Enable / disable open free mode |
| `/setlimit <id> <limit> <days>` | Custom daily quota + expiry (creates premium sub) |
| `/broadcast <msg>` | Message all users |
| `/stats` | System-wide statistics |
| `/active` | Live cloud jobs + stop buttons + recent runs |
| `/approved_users` `/unapproved_users` `/ban_users` `/banned_users` `/premium_users` | User lists |

---

## 9. Limits & Quotas (Free vs Premium vs Admin)

| Resource | Free | Premium (₹99) | Admin |
|---|---|---|---|
| Daily file quota | 30 | custom (e.g., 70 advertised) | Unlimited |
| .so / .dex upload | ≤30 MB | ≤100 MB | 2000 MB |
| APK / ZIP upload | ≤200 MB | ≤500 MB | 2000 MB |
| JADX / dex2jar input | ≤30 MB | ≤100 MB | unlimited |
| PDF → TXT | ≤30 MB | ≤300 MB | unlimited |
| ZIP contents (.so/.dex) | max 1 | max 5 | unlimited |
| ZIP contents (.apk) | max 1 | max 2 | unlimited |
| Smali decode (dex per ZIP) | max 3 | max 10 | unlimited |
| DEX compile (smali files) | max 1000 | max 5000 | unlimited |
| C/C++ compile (source files) | max 5 | max 20 | unlimited |
| PDF ZIP (PDFs per ZIP) | max 5 | max 20 | unlimited |
| Batch binaries per ZIP | max 5 | max 5 | unlimited |
| Concurrent jobs (server) | — | — | 4 total |
| Queue priority | normal | ⚡ fast-lane | ⚡ fast-lane |

**Engine gating:** `apkbuild`, `apksign`, `cccompile`, `dexcompile-*`, `apktool`, `apktool-build` are **premium-only** (`PREMIUM_ONLY_ENGINES`).

---

## 10. Configuration — Environment Variables

### Bot (Railway env)
| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather token |
| `ALLOWED_USER_IDS` | optional | Comma-separated IDs that bypass approval |
| `MAX_FILE_MB` | optional | Max upload size (default 100) |
| `WEBHOOK_URL` | optional | If set → webhook mode; else polling |
| `PORT` | optional | HTTP port (default 8080) |
| `GITHUB_TOKEN` | ✅ | GitHub PAT (repo scope) to trigger/cancel runs & manage secrets |
| `GITHUB_REPO` | optional | `owner/repo` (default `Saini920/Bottestgidra`) |
| `GITHUB_EVENT` | optional | dispatch event name (default `decompile-job`) |
| `FORCE_CHANNEL_2` | optional | Extra force-join channel |
| `RAILWAY_PUBLIC_DOMAIN` | optional | Used to build `/internal/count` report URL |

### GitHub repo secrets (for workers)
| Secret | Used by |
|---|---|
| `TELEGRAM_BOT_TOKEN` | fallback bot token |
| `API_ID`, `API_HASH` | MTProto (Pyrogram) sessions |
| `TG_SESSIONS_B64` | base64 tar of pre-authenticated MTProto sessions (avoids FloodWait) |
| `SIGNKEY_{chat_id}` | per-user custom signing keystore (set via `/setkey`) |

### Worker env (injected by workflows as `PAYLOAD_*`)
`PAYLOAD_FILE_URL`, `PAYLOAD_TG_FILE_PATH`, `PAYLOAD_CHAT_ID`, `PAYLOAD_MESSAGE_ID`, `PAYLOAD_ORIGINAL_MESSAGE_ID`, `PAYLOAD_FILENAME`, `PAYLOAD_JOB_ID`, `PAYLOAD_IS_ADMIN`, `PAYLOAD_IS_PREMIUM`, `PAYLOAD_USER_ID`, `PAYLOAD_REPORT_URL`, `PAYLOAD_FILE_ID`, `PAYLOAD_SMALI_MODE`, `PAYLOAD_DEXCOMPILE_MODE`, `PAYLOAD_NDK_BIN`, `PAYLOAD_SDK_ROOT`, `PAYLOAD_MIN_SDK`, `PAYLOAD_KEYSTORE`, `JAVA_MAX_MEM`, etc.

---

## 11. Data Storage

### `database.json` (via `RepoDB`, on git branch `data`)
Stored in the GitHub repo on a **dedicated branch (`data`)** so every quota save commits with `[skip ci]` and never triggers a Railway redeploy (Railway only watches `main`).

Schema:
```json
{
  "approved": ["<telegram_id>", ...],
  "banned": ["<telegram_id>", ...],
  "subscriptions": { "<id>": { "expires_at": "YYYY-MM-DD", "daily_limit": 50, "warned_5": true, "warned_1": false } },
  "names": { "<id>": "Name (@username)" },
  "daily_usage": { "<id>": { "date": "YYYY-MM-DD", "count": 1 } },
  "free_mode": false,
  "total_files": 0,
  "active_jobs": { "<message_id>": { "user_id": ..., "filename": ..., "engine": ..., "started": ... } }
}
```

### GitHub encrypted secrets (signing keys)
Custom keystores are stored per-chat as repo secrets `SIGNKEY_{chat_id}` encrypted with the repo's public key via **PyNaCl `crypto_box_seal`** (base64 JSON: `keystore_b64`, `storepass`, `keypass`, `alias`).

---

## 12. GitHub Actions Workflow System (the "cloud server")

The whole compute layer runs on **GitHub Actions** — a clever way to get free, powerful cloud runners:

1. `bot.py` POSTs `repos/{repo}/dispatches` with `event_type` (e.g., `decompile-job`) + `client_payload`.
2. The matching workflow YAML (keyed on `repository_dispatch.types`) starts on `ubuntu-latest`.
3. Toolchains are cached between runs via `actions/cache`:
   - Ghidra → `/opt/ghidra` (key `ghidra-11.3.2-v1`, ~1.2 GB download on first run)
   - JADX → `/opt/jadx` (key `jadx-1.5.6`)
   - dex2jar + CFR → `/opt/dex2jar`, `/opt/cfr.jar` (key `dex2jar-cfr-v2`)
   - MTProto sessions → `/opt/tg_sessions` (key `tg-session-v2`, restored from `TG_SESSIONS_B64` secret)
4. The worker runs inside `python3 -m venv /tmp/venv` with `httpx pyrogram tgcrypto`.
5. Completed runs are **auto-deleted** after 5 minutes by `cleanup_workflows_loop` to conserve Actions quota (and 24-hour lifetime).

Run naming convention: `{prefix}-{chat_id}-{message_id}` → parsed by `parse_run_name()` for `/active` tracking and cancellation.

---

## 13. Security & Access Control

| Layer | Mechanism |
|---|---|
| Force-join | Users must join `@allinformation0173` (+ optional 2nd channel) |
| Approval | New users must request access; admins approve/deny |
| Free mode | Global toggle to bypass approval |
| Bans | Banned IDs blocked at `is_allowed()` |
| Admin gating | Hardcoded admin ID list checks on every admin command/callback |
| Quotas | Daily per-user counters persisted in `RepoDB` |
| Secret storage | Signing keys encrypted (sealed box) into GitHub repo secrets |
| Report endpoint | `/internal/count` requires `X-Count-Token` = bot token |
| File size caps | Enforced both in bot (pre-dispatch) and worker (post-download) |

> ⚠️ **Observation:** The Telegram bot token is passed inside the GitHub `client_payload` to workers — anyone with repo read access could see it in run payloads/logs. Consider keeping it as a repo secret instead.

---

## 14. Error Handling & Resilience Patterns

- **Ghidra crash-retry**: if `analyzeHeadless` exits non-zero, the worker re-runs with `DisableCallFixup` pre-script (known workaround for `CallFixupAnalyzer` crashes on some binaries).
- **Memory management**: `apply_memory_settings()` uncomments `JAVA_MAX_MEM` in `launch.properties` and caps it to `available RAM − 1 GB`; `DecompileAll.java` recycles the decompiler interface every 1500 functions to avoid OOM.
- **Stall watchdog**: every long-running tool has a 30-min CPU-activity timeout (via `/proc/{pid}/stat`); downloads have a 45-s stall detector; uploads retry with FloodWait backoff.
- **Engine fallbacks**: dex2jar → JADX; CFR → JADX; HTTP download → MTProto; HTTP upload → MTProto.
- **Cancellation**: cooperative `CANCELLED` flag checked in all streaming loops + GitHub run cancellation API.
- **Error logs**: worker crashes send an `error.txt` document back to the user with a traceback.
- **409 Conflict**: polling mode retries after 60 s if another instance is polling.

---

## 15. Deployment

### Railway (bot)
1. Push repo to GitHub.
2. Railway → New Project → Deploy from GitHub repo.
3. Set env vars (see §10) — `TELEGRAM_BOT_TOKEN`, `GITHUB_TOKEN`, `GITHUB_REPO` are essential.
4. The Docker image builds from `Dockerfile`; bot runs `bot.py` (webhook or polling + health server on port 8080).

### GitHub repo (workers)
The same repo must contain the workflows + worker scripts + secrets:
- `API_ID`, `API_HASH`, `TG_SESSIONS_B64` (MTProto),
- `TELEGRAM_BOT_TOKEN` (fallback),
- `SIGNKEY_*` secrets created automatically via `/setkey`.

> The GitHub repo effectively acts as the **compute backend** — every decompile job burns one GitHub Actions run. The `cleanup_workflows_loop` deletes finished runs to preserve quota, and large toolchains are cached to keep runs fast.

---

## 16. Notes, Observations & Potential Improvements

1. **Heavy code duplication**: all worker scripts copy the same ~400-line download/upload/progress/error-log boilerplate. A shared `worker_common.py` module would cut maintenance significantly.
2. **Legacy leftovers**: `refactor.py`, `refactor2.py`, `refactor3.py`, and `user_subscriptions.json` are migration artifacts and can be deleted. `database.json` at repo root is a backup copy (the live copy lives on the `data` branch).
3. **Bot token in client_payload** — security risk; prefer a repo secret.
4. **Hardcoded admin IDs & branding** (`@R3V_X`, channel links, `Saini920/Bottestgidra`) — should be moved to env vars for reusability.
5. **Rate limits**: GitHub Actions API + Telegram have rate limits; the 4-job concurrency and 10-min sliding window mitigate this.
6. **Data consistency**: `RepoDB` reads/writes the whole JSON on every change (no locking) — concurrent saves could theoretically race; acceptable at current scale.
7. **Dead code** (minor): `send_document()` in some workers is unused (async upload path is used instead); `refactor3.py` references a `cmd_link` command that no longer exists in `bot.py`.
8. **Commercial design**: the free/premium tiering, fast-lane queue, force-join channels, and admin monetization flows make this a production-grade "paid service" bot rather than a hobby project.

---

*Analysis generated from the source code in `ghidra-telegram-bot-master/` — covers all 25 files including bot, 13 workers, 2 Ghidra Java scripts, 12 GitHub Actions workflows, database layer, and deployment files.*
