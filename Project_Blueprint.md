# VENTER — Serverless Reverse Engineering Web Studio

**Project Blueprint** — Complete Architecture, Features, Workflows, Tools & FAQ

> **One-line summary:** A 100% serverless web platform that lets users upload binaries in the browser, decompile them with Ghidra / JADX / apktool / etc. on free GitHub Actions runners, and store every file privately in their own Telegram "Saved Messages" — with **zero Python and zero backend server**.

---

## 1. Overview & Motivation

### Why we are replacing the old system (Python + Telegram Bot)

| Problem (old system) | Solution (Venter) |
|---|---|
| Telegram Bot API 50 MB upload limit — big files can't be processed | MTProto (mtcute) direct from the browser — **2 GB free / 4 GB premium** |
| Bot token ban risk — if the bot looks spammy, the whole service dies | **No bot at all.** Each user logs in with their own Telegram account (exactly like web.telegram.org) |
| Railway hosting RAM limits → OOM on big files | Heavy compute runs on **free GitHub Actions runners** (4-core / 16 GB) |
| Python glue code (bot + 12 worker scripts) to maintain | **100% TypeScript / Node.js** — one language everywhere |
| Paid server needed for the bot | **Zero backend server** — Cloudflare Pages (free) + GitHub Actions (free) + Telegram (free) |

### Architecture at a glance

```
┌────────────────────────────────────────────────────────────────┐
│  LAYER 1 — FRONTEND (Cloudflare Pages — free, static)          │
│  React + Vite + TypeScript + Tailwind (dark mode)              │
│  • Telegram login (mtcute runs IN THE BROWSER)                 │
│  • File upload → streams to Telegram "Saved Messages"          │
│  • Monaco editor for viewing decompiled code                   │
│  • Live progress via ntfy.sh (SSE)                             │
│  • Settings page: GitHub token, repo link, API_ID/HASH         │
├────────────────────────────────────────────────────────────────┤
│  LAYER 2 — STORAGE (Telegram "Saved Messages" — free)          │
│  • Every file lives in the user's own private chat             │
│  • 2 GB per file (free accounts) / 4 GB (premium)              │
│  • No disk space ever used by the platform                     │
├────────────────────────────────────────────────────────────────┤
│  LAYER 3 — COMPUTE (GitHub Actions — free)                     │
│  • repository_dispatch triggers a worker job per engine        │
│  • worker.js downloads input from Saved Messages               │
│  • Runs Ghidra / JADX / apktool / dex2jar / smali / gcc...     │
│  • Uploads result ZIP back to Saved Messages                   │
│  • Posts live progress to ntfy.sh topic                        │
└────────────────────────────────────────────────────────────────┘
```

### Why this is fully serverless

Every moving part is reachable from the browser with **no backend of our own**:

| Service | Used for | CORS / browser support |
|---|---|---|
| **Telegram MTProto** (mtcute) | Login, upload, download, file listing | ✅ WebSocket in browser (same as web.telegram.org) |
| **GitHub REST API** | `repository_dispatch`, token/repo validation | ✅ CORS enabled |
| **ntfy.sh** | Live job progress push | ✅ SSE/WebSocket from browser |

The only code that ever runs on a "server" is the **worker.js** on GitHub Actions runners — which is free and already the pattern today.

---

## 2. Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Language | **TypeScript everywhere** | One language for frontend + worker; type safety |
| Frontend framework | **React 18 + Vite** | Fast builds, easy Cloudflare Pages deploy |
| Styling | **Tailwind CSS (dark mode)** | Matches blueprint, fast UI dev |
| MTProto client | **mtcute** | Modern TS MTProto library; runs in **browser AND Node** with the same session format |
| Code editor | **Monaco Editor** (@monaco-editor/react) | VS Code-quality C/Java/Smali viewer |
| ZIP handling (browser) | **fflate** | Tiny, fast unzip/zip in the browser |
| ZIP handling (worker) | **adm-zip / archiver** | Node-native zip |
| Progress push | **ntfy.sh** (SSE) | Free pub/sub, already used by current workers |
| GitHub dispatch | **GitHub REST API** (`/dispatches`) | Direct from browser |
| Static hosting | **Cloudflare Pages** | Free, global CDN, custom domain |
| CI runners | **GitHub Actions (ubuntu-latest)** | Free 4-core / 16 GB runners |

**Key decision — mtcute:**
- Browser build handles login (phone → OTP → 2FA) exactly like Telegram's own web client.
- Node build on the worker uses the **same exported session string** to download/upload files.
- Session format is identical on both sides → no conversion layer.

---

## 3. Features

### 3.1 Decompilation & Analysis Engines (the core)

| # | Engine | Input | Output | User tier |
|---|---|---|---|---|
| 1 | ⚙️ **Ghidra** | .exe / .dll / .so / .elf / .apk / .zip / firmware | `decompiled.c` + `info.txt` (strings, symbols, compiler, arch) | Free |
| 2 | ☕ **JADX** | .apk / .dex / .class / .jar / .smali / .zip | Java source code | Free (≤30 MB), Premium (≤100 MB) |
| 3 | 🧬 **dex2jar** | .apk / .dex | JAR + Java source | Free (≤30 MB), Premium (≤100 MB) |
| 4 | 📱 **Apktool** | .apk | XML + Smali (decompile) | Premium |
| 5 | 🧩 **Smali decode** | .dex / .zip with .dex | Smali code | Free (≤3 dex), Premium (≤10 dex) |
| 6 | 🛠️ **DEX compile** | .smali / .java / .jar / .zip | `classes.dex` | Premium |
| 7 | ⚙️ **C/C++ compile** | .c / .cpp / .zip | Android ARM64 `.so` (NDK) | Premium |
| 8 | 📦 **APK build** | source .zip (Java/Kotlin + NDK) | signed + unsigned APK | Premium |
| 9 | 🔏 **APK sign** | .apk | re-signed APK (v1+v2, Android 5–16) | Premium |
| 10 | 📄 **PDF → TXT** | .pdf / .zip of PDFs | plain text | Free (≤30 MB), Premium (≤300 MB) |

### 3.2 Platform features

- 🔐 **Telegram account login** in the browser (phone → OTP → 2FA with password hint), session saved in localStorage.
- 📤 **Direct upload in browser** — file streams straight to the user's Saved Messages (no server RAM used).
- 📦 **Batch / ZIP decompiler** — one ZIP with multiple .so/.dex/.apk, all processed (Premium: max 5 .so/.dex + 2 .apk).
- 🔍 **Smart APK scanner** — automatically extracts and decompiles native `.so` libraries inside an APK.
- 📊 **Live progress** — real-time percentage bar + current step ("Importing into Ghidra…", "Decompiling 12/340 functions…"), pushed over ntfy.sh.
- 🛑 **Stop button** — cancel a running GitHub Actions run from the UI.
- 📁 **Virtual file explorer** — browse all files in Saved Messages; open any result in Monaco.
- ⌨️ **Monaco editor** — syntax-highlighted C / Java / Smali viewer with a file-tree sidebar.
- ⭐ **Premium subscriptions** — daily limits, bigger file caps, premium engines, priority queue.
- 👑 **Admin panel** — approve users, ban, set custom limits, broadcast, stats.
- 🌐 **Multi-user SaaS-ready** — every user has their own Telegram login and their own private storage; no shared server storage.
- 🔧 **Settings page** — user configures GitHub token, repo link, API_ID/API_HASH, and tests the connection (Section 6).

---

## 4. Tools Reference (what actually does the work)

All heavy tools are external binaries invoked by `worker.js` via subprocess — **none of them are Python**:

| Tool | Language | Invocation | Produces |
|---|---|---|---|
| Ghidra | Java | `analyzeHeadless <project> Proj -import <file> -postScript DecompileAll.java out.c info.txt -deleteProject` | decompiled C + info.txt |
| JADX | Java | `jadx -d out_dir --no-res <input>` | .java sources |
| dex2jar | Java | `d2j-dex2jar.sh -f -o out.jar input.dex` | .jar |
| Apktool | Java | `apktool d -f -o out input.apk` / `apktool b -o out.apk dir` | smali/XML or rebuilt APK |
| baksmali / smali | Java | `baksmali d classes.dex -o out` / `smali a out -o classes.dex` | smali / dex |
| d8 / dx | Java | `d8 --release --min-api <n> --output out input.jar` | classes.dex |
| GCC / Clang (NDK) | C/C++ | `aarch64-linux-gnu-gcc -O3 -o out.so in.c` | Android .so |
| Gradle | Java/Kotlin | `./gradlew assembleRelease --parallel` | APK |
| apksigner + zipalign | Java | `zipalign -p -f 4 in.apk out.apk && apksigner sign --ks key.jks …` | signed APK |
| pdftotext | C (poppler) | `pdftotext -layout in.pdf out.txt` | text |

> The Ghidra post-script `DecompileAll.java` stays as-is — it is Java and runs inside Ghidra regardless of the outer language.

---

## 5. User Workflow (end to end)

```
1. OPEN  → user opens the Venter website (Cloudflare Pages)
2. LOGIN → clicks "Login with Telegram"
           → enters phone number (+91…)
           → enters OTP from the Telegram app
           → enters 2FA password if enabled (hint shown)
           → mtcute creates session, saved to localStorage
3. SETTINGS (once) → user pastes GITHUB_TOKEN, GITHUB_REPO, API_ID, API_HASH
           → clicks "Test Connection" → green check
4. UPLOAD → drag & drop a binary (or paste a direct URL)
           → file streams to the user's Saved Messages
5. CHOOSE → platform detects file type (APK / DEX / ZIP / EXE…)
           → user picks an engine (Ghidra, JADX, Apktool, Sign…)
6. RUN   → frontend calls GitHub repository_dispatch (event_type = engine)
           → job runs on a free Actions runner
           → live progress bar updates via ntfy.sh
7. RESULT → worker uploads result ZIP to Saved Messages
           → frontend refreshes file explorer, highlights the new file
8. VIEW  → user clicks the result → Monaco editor opens (C / Java / Smali)
           → or downloads the ZIP directly
9. MANAGE → user can delete files (deletes the Telegram message too)
```

---

## 6. Web Settings — User-Configurable Environment

The **Settings page** lets each user configure their own environment. All values are stored in the browser (localStorage, optionally AES-encrypted).

### 6.1 Fields

| Field | Example | Purpose | Required |
|---|---|---|---|
| `GITHUB_TOKEN` | `github_pat_11A…` | Fine-grained PAT (Actions + Contents on the worker repo) to dispatch jobs & validate | ✅ |
| `GITHUB_REPO` | `username/venter-engine` | Repo that contains `.github/workflows/*.yml` + `workers/` | ✅ |
| `API_ID` | `1234567` | Telegram app ID (from my.telegram.org) | ✅ |
| `API_HASH` | `0123abcd…` | Telegram app hash | ✅ |
| `SESSION_KEY` (optional) | passphrase | Used to encrypt the Telegram session before it is passed to workers | optional |

### 6.2 Test Connection button

When clicked, the frontend calls:

```
GET https://api.github.com/repos/{owner}/{repo}
GET https://api.github.com/rate_limit        (Authorization: Bearer <token>)
GET https://api.github.com/repos/{owner}/{repo}/actions/secrets/public-key
```

and shows a status card:

- 🟢 **Connected** — token valid, repo found, dispatch permission OK
- 🟡 **Repo not found** — check the `owner/repo` spelling
- 🔴 **Token invalid / no permission** — check token scopes (needs `Actions: write`, `Contents: read`)

### 6.3 How settings are used

1. **Dispatch:** `POST /repos/{repo}/dispatches` with `Authorization: Bearer <GITHUB_TOKEN>`.
2. **Worker env:** the dispatch `client_payload` carries the config; worker secrets (`API_ID`, `API_HASH`, `SESSION_KEY`) can also live as GitHub Actions secrets — the frontend always prefers the user's own values.
3. **Multiple repos:** each user can point at their own GitHub account/repo → fully distributed, no shared infrastructure.

---

## 7. Technical Pipeline (job lifecycle)

### 7.1 Dispatch payload

```
POST https://api.github.com/repos/{owner}/{repo}/dispatches
{
  "event_type": "decompile-ghidra",          // one per engine
  "client_payload": {
    "file_id": "123456789_<access_hash>",    // doc in Saved Messages
    "filename": "malware_sample.exe",
    "session": "<encrypted mtcute string session>",
    "job_id": "job_9f3k2",                   // ntfy.sh topic
    "webhook_url": "https://ntfy.sh/job_9f3k2",
    "tool_options": { "arch": "x86_64", "min_sdk": "24" },
    "chat_id": "me"                          // Saved Messages
  }
}
```

### 7.2 Worker steps (worker.js on the runner)

```
1. Parse env from client_payload
2. Login with mtcute (Node) using the session string
3. Download input file from Saved Messages → stream to disk (progress → ntfy)
4. Detect file type → enforce size/zip limits
5. Run the tool (Ghidra / JADX / …) — stream stdout lines → ntfy
6. Parse progress markers (e.g. "DECOMP_PROGRESS 12/340") → ntfy
7. Package result ZIP
8. Upload result ZIP to Saved Messages (progress → ntfy)
9. POST final event → ntfy: { status, file_id, filename, size }
10. Cleanup temp dir
```

### 7.3 Progress & completion

- Worker posts every status line to `https://ntfy.sh/{job_id}` (Title = step).
- Frontend subscribes via **SSE** and renders the progress bar + current step.
- Completion event carries the new `file_id` → frontend lists it from Saved Messages and shows a "View in Monaco" button.

### 7.4 GitHub Actions event types

| Engine | `event_type` |
|---|---|
| Ghidra | `decompile-ghidra` |
| JADX | `decompile-jadx` |
| dex2jar | `decompile-dex2jar` |
| Apktool | `decompile-apktool` |
| Apktool build | `compile-apktool` |
| Smali | `decompile-smali` / `decompile-smali-extract` |
| DEX compile | `dex-compile-smali` / `dex-compile-java` |
| C/C++ compile | `cc-compile` |
| APK source build | `apk-source-build` |
| APK sign | `apk-sign` |
| PDF → TXT | `pdf-to-txt` |

---

## 8. GitHub Actions Workflows

One workflow file per engine (same pattern as today, worker swapped to Node):

```yaml
name: decompile-ghidra
run-name: job-${{ github.event.client_payload.job_id }}
on:
  repository_dispatch:
    types: [decompile-ghidra]

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 1440
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Node 22
        uses: actions/setup-node@v4
        with:
          node-version: '22'

      - name: Setup Java 21
        uses: actions/setup-java@v4
        with:
          distribution: temurin
          java-version: '21'

      - name: Cache Ghidra
        uses: actions/cache@v4
        with:
          path: /opt/ghidra
          key: ghidra-11.3.2-v1

      - name: Get Ghidra
        if: steps.ghidra-cache.outputs.cache-hit != 'true'
        run: |
          curl -L --retry 3 -o /tmp/ghidra.zip \
            "https://github.com/NationalSecurityAgency/ghidra/releases/download/..."
          sudo unzip -q -o /tmp/ghidra.zip -d /opt
          # ... normalize to /opt/ghidra

      - name: Install deps
        run: npm ci --prefix workers

      - name: Run worker
        env:
          PAYLOAD: ${{ toJson(github.event.client_payload) }}
          API_ID: ${{ secrets.API_ID }}
          API_HASH: ${{ secrets.API_HASH }}
          SESSION_KEY: ${{ secrets.SESSION_KEY }}
        run: node workers/ghidra.js
```

Worker tool installs stay **identical** to today (Java 21, Ghidra cache, JADX, apktool, NDK…).

---

## 9. Data Storage & Security

### 9.1 What is stored where

| Data | Location | Notes |
|---|---|---|
| Telegram session (mtcute string session) | Browser **IndexedDB** (not localStorage) | AES-256-GCM encrypted; key derived from user passphrase / 2FA password — key itself is never stored |
| GitHub token, repo, API_ID/HASH | Browser IndexedDB | Never sent to any server except the user's own GitHub repo |
| Files | Telegram "Saved Messages" | Private per user, 2 GB / 4 GB per file |
| Jobs / usage / subscriptions | GitHub repo `database.json` (branch) **or** SQLite in the worker repo | Same pattern as today |
| Live job state | ntfy.sh topic `{job_id}` | Random UUID, ephemeral, auto-expiring |

### 9.2 Threat model

| # | Risk | Impact | Severity | Mitigation |
|---|---|---|---|---|
| 1 | Session theft via XSS (browser storage) | Attacker gets full Telegram account access | 🔴 Critical | Encrypted storage (key never stored), strict CSP, IndexedDB |
| 2 | Session leak in GitHub dispatch history / logs | Account takeover from repo history | 🔴 High | Session sent as AES-256-GCM blob; decrypted only inside worker with `SESSION_KEY` secret |
| 3 | Malicious binary exploits tool (e.g. Ghidra CVE → RCE on runner) | Session steal from runner | 🟡 Medium | Ephemeral runners (fresh VM per job, self-destruct), session never passed to tool subprocess |
| 4 | GitHub token leak | Unauthorized dispatch on repo | 🟡 Medium | Fine-grained PAT scoped to one repo, short expiry, "Test Connection" scope check |
| 5 | Zip-slip (path traversal) via malicious ZIP | File overwrite on runner | 🟡 Medium | Filename sanitization on every ZIP extract (`../` entries rejected) |
| 6 | Session replay after logout | Stolen session still usable | 🟡 Medium | `auth.logOut()` kills the session server-side; "Log out everywhere" revokes all |
| 7 | Job abuse / spam | Quota exhaustion | 🟢 Low | Daily quota (30/70), max 4 concurrent jobs, random UUID ntfy topics |

### 9.3 Session encryption flow (implemented)

```
Browser (login):
  mtcute session → AES-256-GCM encrypt
  key = HKDF(user passphrase OR 2FA password + random salt)   ← key NEVER stored
  encrypted blob → IndexedDB

Dispatch:
  client_payload.session = encrypted blob (never plaintext)

Worker (GitHub Actions):
  blob → AES-256-GCM decrypt with SESSION_KEY (GitHub Actions secret)
  → mtcute login → download/upload → session kept in-process only
  → session NEVER written to disk, env, subprocess args, or logs
```

### 9.4 Login hardening (implemented)

- **2FA required** — accounts without two-step verification get a warning and are blocked from login (same policy Telegram itself uses).
- **Active sessions list** — Settings shows all authorizations via `account.getAuthorizations`; user can revoke any device.
- **"Log out everywhere"** button — one click terminates all sessions.
- **Logout = server-side kill** — `auth.logOut()` destroys the Telegram session; clearing browser storage alone is never enough.

### 9.5 Frontend security (implemented)

- **Strict Content-Security-Policy** — only whitelisted origins (Telegram MTProto, GitHub API, ntfy.sh); no inline scripts, no third-party JS.
- **IndexedDB** instead of localStorage (async, less visible to simple XSS scrapers) for all settings + session.
- **HTTPS everywhere** — free via Cloudflare.
- **No server-side code** → no SQL injection / server RCE attack surface at all; attack surface is limited to client-side JS.
- **Supply chain** — npm dependencies version-pinned, `npm audit` runs in CI.
- **No secrets in frontend code** — API_ID/HASH/token come from user settings, never hardcoded in the bundle.

### 9.6 Worker security (implemented)

- **Ephemeral runners = isolation win** — every job runs on a fresh VM that self-destructs; even if a malicious binary exploits a parsing tool, the damage is one-use and gone.
- **Zip-slip protection** — all ZIP extractions sanitize entry names; `../` and absolute paths are rejected.
- **Session hygiene** — decrypted session lives only in worker process memory; never in subprocess env, CLI args, files, or logs.
- **GitHub secret masking** — `API_ID`, `API_HASH`, `SESSION_KEY` are auto-masked in Actions logs.
- **Limits before processing** — file size and ZIP content checks run before any tool is invoked.

### 9.7 Token & settings security (implemented)

- **Fine-grained GitHub PAT** scoped to exactly one repo, with short expiry → leaked token = small blast radius.
- **"Test Connection"** validates token expiry + repo access + `Actions: write` / `Contents: read` scopes before any job runs.
- **User warning** in Settings UI: token only works on the user's own repo; recommends a dummy GitHub account.
- `SESSION_KEY` lives as a **GitHub Actions secret** — the browser never sees the worker's key, and the worker never sees the browser's key.

### 9.8 Abuse prevention (implemented)

- Daily quota: 30 files free / 70 premium; max 4 concurrent jobs per repo.
- ntfy.sh topics are **random UUIDs per job** — cannot be guessed or spammed.
- File size + ZIP content limits enforced in the worker before any processing.

### 9.9 Implementation priority

| # | Task | Phase |
|---|---|---|
| 1 | Session AES-256-GCM encryption + key derivation (HKDF) | Phase 1 |
| 2 | 2FA enforcement + active-sessions list + log-out-everywhere | Phase 1 |
| 3 | Strict CSP + IndexedDB storage layer | Phase 1 |
| 4 | Zip-slip protection in all workers | Phase 3 |
| 5 | `npm audit` + pinned dependencies in CI | Phase 1 |
| 6 | Full security audit + 1 GB APK stress test | Phase 5 |

> **Honest note:** no client-side app is 100% bulletproof — if an attacker gains full control of the user's browser (malware), nothing can fully protect the session. Defense-in-depth (2FA + encrypted storage + ephemeral runners + strict CSP) is the industry-standard mitigation, the same approach Telegram's own web client uses.

---

## 10. Frontend Structure (planned)

```
frontend/
├── src/
│   ├── main.tsx
│   ├── App.tsx
│   ├── lib/
│   │   ├── telegram.ts        # mtcute login, upload, download, list
│   │   ├── github.ts          # dispatch, token validation
│   │   ├── ntfy.ts            # SSE progress subscription
│   │   ├── storage.ts         # localStorage (encrypted settings/session)
│   │   └── crypto.ts          # AES-256-GCM helpers
│   ├── components/
│   │   ├── LoginFlow.tsx      # phone → OTP → 2FA wizard
│   │   ├── SettingsPanel.tsx  # GitHub token, repo, API_ID/HASH, Test Connection
│   │   ├── UploadDropzone.tsx
│   │   ├── EnginePicker.tsx   # auto-detect + engine buttons
│   │   ├── JobProgress.tsx    # live ntfy progress bar
│   │   ├── FileExplorer.tsx   # Saved Messages browser
│   │   └── MonacoViewer.tsx   # code viewer + file tree
│   └── pages/
│       ├── Dashboard.tsx
│       ├── Settings.tsx
│       └── Admin.tsx
└── workers/                   # worker.js scripts (also used by Actions)
```

---

## 11. Migration Map — Python → TypeScript

| Old (Python) | New (TypeScript) | Status |
|---|---|---|
| `bot.py` (python-telegram-bot) | React frontend + browser mtcute | replaced entirely |
| `worker.py` (Ghidra) | `workers/ghidra.js` | rewrite (same subprocess calls) |
| `worker_jadx.py` | `workers/jadx.js` | rewrite |
| `worker_apktool*.py` | `workers/apktool*.js` | rewrite |
| `worker_dex2jar.py`, `worker_smali.py`, `worker_dex_compile.py` | `workers/dex*.js` | rewrite |
| `worker_cc_compile.py` | `workers/cc.js` | rewrite |
| `worker_apk_build.py`, `worker_apk_sign.py` | `workers/apk*.js` | rewrite |
| `worker_pdf_txt.py` | `workers/pdftxt.js` | rewrite |
| `download_file.py` / `upload_file.py` (Pyrogram) | mtcute (browser + node) | replaced |
| `database.py` + `database.json` (GitHub branch) | SQLite in worker repo / GitHub JSON | replace |
| `ghidra_scripts/DecompileAll.java` | unchanged | ✅ stays |
| `.github/workflows/*.yml` | same pattern, `node workers/*.js` | minor edits |

---

## 12. FAQs

**Q1: Kya yeh really server-less hai? Koi backend server nahi?**
Haan. Frontend Cloudflare Pages par static hai. Telegram login browser me mtcute se hota hai. GitHub dispatch aur ntfy.sh dono browser se CORS-supported hain. Sirf worker.js GitHub Actions runner par chalta hai — jo free hai aur user ka apna account hai.

**Q2: 2 GB file browser se upload hogi? Server RAM to exhaust nahi ho jayegi?**
Nahi. File stream chunk-by-chunk (512 KB) browser se seedha Telegram par jaati hai — kabhi kisi server ki RAM ya disk par nahi. Browser streaming MTProto ko sambhalta hai waisa hi jaisa Telegram ka web client karta hai.

**Q3: Telegram session itni sensitive hai — kaise safe rahegi?**
Session browser localStorage me rehta hai aur AES-256 se encrypt hota hai. Worker tak encrypted blob jaata hai jo sirf GitHub Actions secret `SESSION_KEY` se decrypt hota hai. Session kabhi log/URL me nahi aata.

**Q4: GitHub token expiry / rate-limit ho jaye to?**
Settings me "Test Connection" har baar token validity aur rate limit check karta hai. Public repo par GitHub Actions free hai (unlimited); private repo par 2,000 min/month. User kisi bhi naye dummy GitHub account ka token use kar sakta hai.

**Q5: Agar user upload ke baad browser band kar de?**
Upload cancel ho jayega, lekin agar job already dispatch ho chuka hai to runner poora task complete karega aur result Saved Messages me aa jayega. Dobara browser kholne par file explorer me result dikhega.

**Q6: FloodWait / rate limits kaise handle hongi?**
mtcute me built-in FloodWait handler hai — worker auto sleep karke retry karta hai. UI par "Rate limited, pausing 15s…" ka live status dikhega.

**Q7: Kya multiple users ke liye SaaS bana sakte hain?**
Bilkul. Har user apna Telegram account login karta hai, apni files apne Saved Messages me, apna GitHub token/repo use karta hai. Koi centralized storage/server nahi — platform naturally scale karta hai.

**Q8: Kya mujhe ab bhi Python chahiye?**
Nahi. Ek bhi Python file nahi bachegi. Sab kuch TypeScript/Node.js hai. Tools (Ghidra/JADX/etc.) to waise bhi Java/C hain.

**Q9: Deploy kaise hoga?**
- Frontend → **Cloudflare Pages** (repo connect karo, build dir `frontend/dist`)
- Workflows + workers → **user ka apna GitHub repo** (e.g. `username/venter-engine`)
- Settings me user apna repo + token daalta hai — platform ko khud kuch host nahi karna padta.

**Q10: Result code kaise dekhenge?**
File explorer me result ZIP par click karo → frontend usse Saved Messages se download karke fflate se unzip karta hai → Monaco editor me file-tree ke saath syntax-highlighted code dikhta hai.

**Q11: Purana Telegram bot system ka kya hoga?**
Venter launch hone tak purana system chalta rahe sakta hai. Migration map (Section 11) har file ka replacement dikhata hai — worker scripts ek-ek karke port ho sakti hain.

**Q12: Kya user apna koi bhi GitHub repo use kar sakta hai?**
Haan — repo me bas `.github/workflows/` (engine workflows) aur `workers/` folder hona chahiye. User khud repo bana kar template copy kar sakta hai.

---

## 13. Development Roadmap

### Phase 1 — Foundation (Days 1–2)
- [ ] Vite + React + TS + Tailwind scaffold, Cloudflare Pages connect
- [ ] mtcute browser login flow (phone → OTP → 2FA), session in localStorage
- [ ] Settings page: GitHub token / repo / API_ID / HASH + Test Connection

### Phase 2 — Storage engine (Days 3–5)
- [ ] Stream upload → Saved Messages (with progress)
- [ ] Saved Messages file explorer (list, download, delete)
- [ ] fflate unzip + Monaco viewer for results

### Phase 3 — The engine room (Days 6–8)
- [ ] `workers/ghidra.js` (port of worker.py) + workflow YAML
- [ ] Dispatch from browser + ntfy.sh live progress + stop button
- [ ] Port JADX + dex2jar workers

### Phase 4 — Full engine suite (Days 9–12)
- [ ] Port apktool, smali, dex-compile, cc-compile, apk-build, apk-sign, pdf-txt
- [ ] File-type auto-detection + engine picker UI
- [ ] Batch ZIP processing, smart APK .so scanner

### Phase 5 — Platform & launch (Days 13–15)
- [ ] Premium subscriptions + daily quotas + admin panel
- [ ] Session/token encryption hardening, CSP, security audit
- [ ] E2E stress test with a 1 GB APK; docs + deploy guide

---

## 14. File / Repo Layout (monorepo template)

```
venter/                          # user's GitHub repo (e.g. username/venter-engine)
├── .github/workflows/
│   ├── ghidra.yml
│   ├── jadx.yml
│   ├── dex2jar.yml
│   ├── apktool.yml
│   ├── apktool_build.yml
│   ├── smali.yml
│   ├── dex_compile.yml
│   ├── cc_compile.yml
│   ├── apk_build.yml
│   ├── apk_sign.yml
│   └── pdf_txt.yml
├── workers/
│   ├── lib/
│   │   ├── mtcute-client.js    # session login, download, upload
│   │   ├── ntfy.js             # progress publisher
│   │   ├── limits.js           # size / zip / quota checks
│   │   └── crypto.js           # AES-256-GCM session decrypt
│   ├── ghidra.js
│   ├── jadx.js
│   ├── dex2jar.js
│   ├── apktool.js
│   ├── smali.js
│   ├── dex-compile.js
│   ├── cc-compile.js
│   ├── apk-build.js
│   ├── apk-sign.js
│   └── pdf-txt.js
├── frontend/                    # Cloudflare Pages build root
│   ├── src/…
│   ├── package.json
│   └── vite.config.ts
└── README.md
```

---

*Generated blueprint for the Venter serverless reverse-engineering web studio. Supersedes the Python/Telegram-bot architecture.*
