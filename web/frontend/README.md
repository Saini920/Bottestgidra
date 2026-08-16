# Frontend — Next.js/React

Login wizard (phone → OTP → **2FA password screen**), storage manager, and RE tools UI.
All progress is real-time via WebSocket.

## Setup

```bash
cp .env.local.example .env.local   # set NEXT_PUBLIC_API_URL
npm install
npm run dev        # http://localhost:3000
```

## Env

| Variable | Default | Purpose |
|---|---|---|
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend base URL |

## Pages

- `/` — login wizard (MTProto: send-code → verify-code → check-password when 2FA on)
- `/dashboard` — account overview
- `/storage` — file manager (files live in the user's Telegram Saved Messages)
- `/tools` — RE engines (dispatch to GitHub Actions, live progress, results)

## Deploy on Cloudflare Pages

The app is a **static export** (`output: "export"` → `out/`). Every page is a
client component that talks to the backend directly, so Cloudflare Pages only
serves static files — no server functions needed.

### Via GitHub (recommended)
1. Push this repo to GitHub.
2. Cloudflare Dashboard → Workers & Pages → **Create → Pages → Connect to Git**.
3. Pick the repo, then set:
   - **Root directory**: `web/frontend`
   - **Build command**: `npm run build`
   - **Build output directory**: `out`
   - Framework preset: *None* (manual settings above)
4. **Environment variable**: `NEXT_PUBLIC_API_URL` = `https://your-backend.example.com`
   (the deployed FastAPI backend — see below).
5. Deploy. Each push to `main` rebuilds the site automatically.

### Via CLI
```bash
cd web/frontend
npm install
npx wrangler login
npm run build
npx wrangler pages deploy out --project-name=tg-web
```

## ⚠️ Backend cannot live on Cloudflare Pages

The **backend (FastAPI)** needs a real server because it runs:
- **Pyrogram MTProto** — long-lived Telegram sessions (login, storage)
- **WebSocket** live progress
- **SQLite** + file temp storage
- Outbound GitHub API calls

Deploy the backend on **Railway / Render / Fly.io / VPS** (`web/backend`,
`python run.py`), then point `NEXT_PUBLIC_API_URL` at it. The browser connects
to the backend directly (REST + WebSocket), so Cloudflare Pages is only the
frontend.
