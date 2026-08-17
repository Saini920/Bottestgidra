# Venter — Serverless Reverse-Engineering Web Studio

**100% TypeScript / Node.js — zero Python, zero backend server.**

Decompile binaries (Ghidra / JADX / apktool / …) from your browser. Files live
in your private Telegram **Saved Messages**, heavy compute runs on **free GitHub
Actions** runners, and the UI is served from **Cloudflare Pages**.

```
┌────────────────────────────────────────────────────────┐
│ Cloudflare Pages — React frontend (free)               │
│  • mtcute login in browser (OTP + 2FA)                 │
│  • upload → Saved Messages, file explorer, Monaco      │
│  • Settings: GitHub token/repo + Test Connection       │
├────────────────────────────────────────────────────────┤
│ Telegram — storage (free, 2 GB/file)                   │
├────────────────────────────────────────────────────────┤
│ GitHub Actions — worker.js per engine (free 4-core)    │
│  • download input, run tool, upload result ZIP         │
│  • progress → ntfy.sh (frontend SSE)                   │
└────────────────────────────────────────────────────────┘
```

## Project structure

```
repo root
├── frontend/                  # Cloudflare Pages app (React + Vite + TS + Tailwind)
│   ├── src/
│   │   ├── lib/               # telegram (mtcute), github, ntfy, crypto, storage
│   │   ├── components/        # LoginFlow, SettingsPanel, UploadDropzone,
│   │   │                      # EnginePicker, JobProgress, FileExplorer, MonacoViewer
│   │   └── App.tsx            # tabs + flow wiring
│   └── public/_headers        # strict CSP (security section)
├── workers/                   # GitHub Actions worker scripts (Node ESM)
│   ├── ghidra.js              # ✅ ported (worker.py → TS)
│   └── lib/                   # tg (mtcute), ntfy, crypto, limits, zip, ghidra
├── ghidra_scripts/            # DecompileAll.java + DisableCallFixup.java (Java, unchanged)
├── .github/workflows/         # one workflow per engine (ghidra.yml ✅) — MUST be at repo root
├── package.json               # worker dependencies (@mtcute/node, adm-zip)
├── wrangler.toml              # Cloudflare Pages config
└── Project_Blueprint.md       # full architecture + security spec
```

## Deploy — step by step

### 1. GitHub repo (workers + workflows)

1. Push this repo (`.github/workflows/` + `workers/` at repo root — GitHub Actions
   only reads workflows from the root).
2. In repo **Settings → Secrets and variables → Actions**, add:
   | Secret | Value |
   |---|---|
   | `API_ID` | your Telegram app id (my.telegram.org) |
   | `API_HASH` | your Telegram app hash |
   | `SESSION_KEY` | a strong passphrase (same one the user enters in the web Settings) |
3. Create a **fine-grained PAT** with `Actions: write` + `Contents: read` on that repo.

### 2. Cloudflare Pages (frontend)

**Option A — dashboard (recommended):** Cloudflare dashboard → Pages → Create →
connect the repo →

| Setting | Value |
|---|---|
| Root directory | `frontend` |
| Build command | `npm install && npm run build` |
| Build output | `dist` |

**Option B — wrangler CLI:**
```bash
npm i -g wrangler
cd frontend && npm install && npm run build && cd ..
wrangler pages deploy frontend/dist --project-name venter
```

### 3. Use the site

1. Open the Cloudflare Pages URL.
2. Settings me `API_ID`, `API_HASH`, `GITHUB_TOKEN`, `GITHUB_REPO`, `SESSION_KEY` daalo → **Test Connection**.
3. Login with your Telegram number (OTP + 2FA).
4. Upload a binary → pick an engine → watch live progress → open the result in Monaco.

## Why no Python?

Every Python file of the old system has a TypeScript replacement (see
`Project_Blueprint.md` §11 migration map). The tools themselves (Ghidra, JADX,
apktool) are Java/C binaries — they were never Python.

## Security highlights

- Telegram session encrypted with AES-256-GCM; key (SESSION_KEY) never stored in the browser
- 2FA accounts only; "log out everywhere" revokes sessions server-side
- Strict CSP on Cloudflare Pages (`frontend/public/_headers`)
- Zip-slip protection in every worker extraction
- Session passed to workers only as an encrypted blob; decrypted in-process on ephemeral runners

## Verify points before going live

- `workers/lib/tg.js` and `frontend/src/lib/telegram.ts` contain `VERIFY` marks
  on mtcute API calls — run `npm ci` + a typecheck (`npm run typecheck`) and
  confirm signatures against the installed `@mtcute/*` versions.
- Run `npm run check` (worker syntax) and `npm run build` (frontend) locally.
