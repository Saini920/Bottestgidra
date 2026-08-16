# Backend — FastAPI + Pyrogram

MTProto login (phone → OTP → **2FA password screen**), storage API (files kept in the
user's own Telegram Saved Messages), and RE jobs API (GitHub Actions dispatch).

## Setup (no .env needed — configure from the web)

```bash
cd backend
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python run.py           # http://localhost:8000
```

Then open the web app:
1. **/setup** — first-run wizard: enter `API_ID` + `API_HASH` (and optionally
   `GITHUB_TOKEN`, `GITHUB_REPO`). No .env files anywhere.
2. Log in with your Telegram account — **the first user becomes admin**.
3. **/settings** (admin) — change any setting later (GitHub token, repo, webhook
   token, public URL, admin IDs). All values are stored **encrypted at rest**
   (key auto-generated in `secret.key`, or `SESSION_SECRET` env if you prefer).

A `.env` file still works if you ever want it — env vars take priority over
web-configured settings.

## Endpoints

### Auth (MTProto — the 2FA flow)
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/auth/send-code` | `{phone}` | code sent to user's Telegram |
| POST | `/auth/verify-code` | `{phone, code}` | returns `password_required: true` if 2FA is on |
| POST | `/auth/check-password` | `{phone, password}` | only called when 2FA is on |
| POST | `/auth/logout` | — | revokes the Telegram session |
| GET | `/auth/me` | — | current user (Bearer token) |

### Storage (files → user's Saved Messages, server keeps metadata only)
| Method | Path | Notes |
|---|---|---|
| POST | `/storage/upload` | multipart `file` + `channel` (ws progress) |
| GET | `/storage/files` | metadata list (syncs new docs from Telegram) |
| GET | `/storage/download/{file_id}` | streams from Telegram (pass `?channel=` for progress) |
| DELETE | `/storage/files/{file_id}` | deletes from Telegram + DB |

### Jobs (GitHub Actions compute — same workflows as the bot project)
| Method | Path | Notes |
|---|---|---|
| GET | `/api/engines` | engine list for the chooser |
| POST | `/api/jobs` | multipart `file` + `engine` + `channel` |
| GET | `/api/jobs` | user's jobs |
| GET | `/api/jobs/{id}/result` | result ZIP |
| POST | `/api/jobs/{id}/stop` | cancels the GitHub Actions run |
| POST | `/api/status` | **worker webhook** (needs `X-Count-Token` = `WEBHOOK_TOKEN`); JSON progress or multipart result |
| GET | `/api/transfer/...` | signed URL for workers to download the input file |

### Realtime
`GET /ws/progress/{channel}` — subscribe to live progress (upload/download/job channels).

## Worker webhook (Phase 2 — workers report back)

Workers POST to `{PUBLIC_URL}/api/status` with header `X-Count-Token: {WEBHOOK_TOKEN}`:

```json
{ "job_id": 42, "pct": 45, "label": "🔧 Analyzing..." }
```

Result upload (multipart): `job_id` + `file` fields → stored as `result_{job_id}.zip`.
