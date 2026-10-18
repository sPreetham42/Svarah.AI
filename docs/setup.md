# Svarah.ai — Setup and Configuration

Everything below runs **fully offline in mock mode** with no provider keys. Live
providers are opt-in and never required for the demo.

## Requirements

| Tool | Version | Needed for |
| --- | --- | --- |
| Python | 3.12 | backend |
| Node.js | 20+ | web frontend |
| FFmpeg | any recent build | reel composition (mock reels work without it only if `ffmpeg` is on PATH) |
| Tesseract | optional | OCR cross-check of posters. **Absent → that check reports `NEEDS_REVIEW`, never a silent pass.** |

## 1. Backend

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt

# .env lives at the repository root (copy it from .env.example).
# Generate a seal secret and an auth secret:
python -c "import secrets; print(secrets.token_urlsafe(48))"

.\.venv\Scripts\python.exe -m pytest                     # hermetic, never calls a provider
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Health check: `GET http://localhost:8000/api/health` → `{"status":"ok","mode":"mock",…}`.
Interactive docs: `http://localhost:8000/docs`.

On startup the backend logs a warning if `TITAN_SEAL_SECRET` is unset (fact
locking is refused) or if the auth key is ephemeral. Neither is fatal in mock
mode; a missing durable auth key **is** fatal to login in live mode.

## 2. Web frontend

```powershell
cd frontend
npm install
npm run dev            # http://localhost:5173  (Vite proxies /api -> :8000)
```

Build/typecheck: `npm run build`. The dev server proxies `/api` to
`http://localhost:8000`; set `VITE_API_BASE` (see `apps/web/.env.example`) to
point at another origin.

## 3. Android app download

The Android app is built outside this repository. To offer it on the website,
put the APK at `frontend/apps/web/public/downloads/svarah-ai.apk` and redeploy:
the `/download` page and the "Get the app" button pick it up automatically.

## Configuration variables

Documented in `.env.example`. Names only — never commit values.

### Core

| Variable | Default | Purpose |
| --- | --- | --- |
| `TITAN_MODE` | `mock` | `mock` (offline) or `live` |
| `TITAN_DATABASE_URL` | `sqlite:///./titan.db` | database; SQLite locally, Postgres-compatible schema |
| `TITAN_ASSETS_DIR` | `./assets_store` | filesystem asset root |
| `TITAN_CORS_ORIGINS` | `http://localhost:5173,…` | allowed browser origins |

### Integrity and auth (required for a real lock + stable sessions)

| Variable | Purpose |
| --- | --- |
| `TITAN_SEAL_SECRET` | HMAC-SHA256 key for the fact lock and certificate. Empty → locking refused. |
| `TITAN_AUTH_SECRET` | Signs session tokens. Empty → mock mints a per-process key (sessions end on restart); live refuses to issue sessions. |
| `TITAN_AUTH_TOKEN_TTL_HOURS` | Session lifetime (default 8760 = one year). The website renews the session on every visit (`POST /api/auth/refresh`), so an owner stays signed in until they sign out. |
| `TITAN_ALLOW_DEMO_LOGIN` | Mock-mode password-less demo sign-in. Ignored in live mode. |
| `TITAN_MAX_REQUEST_BYTES` | Body ceiling before parsing (default 32 MiB). |

### Providers (leave blank in mock mode)

| Variable | Purpose |
| --- | --- |
| `TITAN_STT_PROVIDER` | `sarvam` \| `auto` \| `mock` \| `none`. Sarvam is the only speech provider; in live mode `auto` = `sarvam`. |
| `TITAN_SARVAM_API_KEY` (+ optional `_2`, `_3`, `_4` backups), `TITAN_SARVAM_API_BASE`, `TITAN_SARVAM_STT_MODEL` | Sarvam speech-to-text, text-to-speech (`bulbul:v3`) and voice cloning. Backup keys are used in order when a key is rejected, out of credits or rate limited. |
| `TITAN_EXTRACTION_PROVIDER` | `auto` \| `mock` \| `agnes` \| `none` |
| `TITAN_AGNES_API_BASE`, `TITAN_AGNES_API_KEY`, `TITAN_AGNES_*_MODEL` | Agnes text (extraction, validation, Campaign Director), image and video |
| `TITAN_VOICE_PROVIDER` | `auto` \| `mock` \| `sarvam` \| `none` |
| `TITAN_WINDSOR_MCP_URL`, `TITAN_WINDSOR_AUTH_MODE`, `TITAN_WINDSOR_API_KEY` | publishing via Windsor MCP |

A template placeholder (`YOUR_…`, `PASTE_…`, `REPLACE_…`) is treated as
*unconfigured*. Provider keys only take effect in `TITAN_MODE=live`; there is
no separate verification flag.

### Budgets and flags

`TITAN_MAX_POSTERS`, `TITAN_MAX_VIDEO_JOBS_PER_CAMPAIGN`, `TITAN_MAX_COPY_VARIANTS`,
`TITAN_MAX_VOICE_VARIANTS`, `TITAN_MAX_VIDEO_VARIANTS`,
`TITAN_ENABLE_AGNES_VIDEO`, `TITAN_ENABLE_DEMO_SABOTAGE`, and the `TITAN_TASKS_*`
/ `TITAN_CLOUD_*` durable-job settings.

## Switching to live providers

1. Set `TITAN_MODE=live` and a durable `TITAN_AUTH_SECRET`.
2. Set `TITAN_AGNES_API_KEY` and `TITAN_SARVAM_API_KEY` in `.env`.
3. Restart the backend. Startup logs and `GET /api/modes` report exactly which
   providers are configured. Nothing falls back to a mock in live mode — an
   unconfigured or failing provider returns an error with its reason.
4. Run `backend/scripts/smoke_live.py` (add `--video` for the video model) to confirm
   account access and quota. These are real, billable calls.
5. Publishing stays sandbox-only until Windsor is configured **and** you approve
   each publication explicitly.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `Failed to fetch` / backend offline banner | backend not running on :8000, or wrong `VITE_API_BASE` |
| Login says auth unavailable | `TITAN_AUTH_SECRET` missing in live mode |
| “fact lock disabled” pill | `TITAN_SEAL_SECRET` empty — generate one |
| Postcards/assets fail | check `TITAN_MAX_POSTERS` budget and that `assets_store` is writable |
| Video job fails | FFmpeg not on `PATH`; install it or use posters/captions |
| OCR checks `NEEDS_REVIEW` | Tesseract not installed — expected; use the human-decision flow |
| Sessions end on restart | `TITAN_AUTH_SECRET` unset (per-process key) |

## Fresh start

The dev SQLite file and the asset store can be reset by deleting
`backend/titan.db` and `backend/assets_store/` — both are recreated on startup.
Older databases are upgraded in place: `app/db.py` appends post-release columns
(`campaigns.owner_uid`, `shops.owner_uid`) without touching existing rows.
