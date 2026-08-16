# Telegram Web — RE Tools + Storage

Web app for the Ghidra Telegram Bot project: MTProto login (phone → OTP → 2FA password),
file storage in the user's own Telegram Saved Messages, and all 13 RE engines
(Ghidra, JADX, Apktool, Smali, DEX compile, NDK C/C++, APK build/sign, PDF→TXT)
running on GitHub Actions — fully real-time via WebSocket.

## Structure

```
web/
├── backend/   FastAPI + Pyrogram (MTProto login, storage API, jobs API, WebSocket)
└── frontend/  Next.js/React (login wizard, storage manager, RE tools UI)
```

See `backend/README.md` and `frontend/README.md` for setup.

## Deployment split

| Part | Where it runs | Why |
|---|---|---|
| `frontend/` | **Cloudflare Pages** (static export, `out/`) | Pure client-side app; no server needed |
| `backend/` | **Railway / Render / Fly.io / VPS** | Needs Pyrogram (long-lived MTProto), WebSocket, SQLite |

The browser talks to the backend directly (REST + WebSocket) — Cloudflare
Pages only serves the static frontend. Set `NEXT_PUBLIC_API_URL` to the
backend's public URL during the frontend build.
