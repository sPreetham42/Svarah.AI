# Svarah.ai — AI marketing campaigns from a shopkeeper's voice

**Say the offer. Get the poster, captions, voice-over and video.**

Svarah.ai turns a spoken offer ("20% off cold coffee this weekend, 4 to 8 PM")
into a complete, multilingual marketing package for a small business. The owner
confirms the facts by voice, and everything generated afterwards is checked
against those locked facts before it can be published.

> **AI creates the marketing. The business owner controls the truth.**

## Features

- **Voice-first** — push-to-talk capture and confirmation (Sarvam Saaras STT).
- **Fact integrity** — facts are extracted (Agnes), validated, and locked with
  a SHA-256 hash + HMAC seal; every asset is verified against them.
- **Auto poster** — art-directed poster built only from the locked facts.
- **Captions** for Instagram, Facebook, X and WhatsApp.
- **Voice-over** in Indian languages (Sarvam Bulbul TTS).
- **Campaign video + AI video** (Magic Hour, or Agnes video).
- **Publishing** to the owner's own social accounts via Upload-Post, after
  explicit approval.
- **Mock and live modes** — the whole flow runs offline in mock mode.

## How it works

```text
voice → Sarvam STT → Agnes fact extraction → validation → owner says "yes"
      → LOCK (hash + seal) → Campaign Director plan
      → poster · captions · voice-over · video   (in parallel)
      → Guardian verification → owner approval → publish / export
```

## Repository layout

```text
.
├── backend/                 FastAPI API (Python 3.12)
│   ├── app/
│   │   ├── api/             HTTP routers (campaigns, factsheets, plans, assets, …)
│   │   ├── services/        provider clients (Agnes, Sarvam, Magic Hour, …) and pipelines
│   │   ├── guardian/        fact verification and certificates
│   │   └── main.py          application factory
│   ├── tests/               pytest suite (hermetic — never calls a provider)
│   ├── scripts/             smoke tests (smoke_live.py makes real, billable calls)
│   └── Dockerfile           production image (used by Render)
├── frontend/                npm workspaces
│   └── apps/web/            React + Vite website and voice workspace (Vercel)
├── docs/                    architecture, setup, API, security
├── .env.example             backend configuration template
└── render.yaml              Render blueprint for the API
```

## Quickstart

Requirements: Python 3.12, Node.js 20+, FFmpeg (for the campaign video).

```powershell
# 1. Configuration (repository root)
Copy-Item .env.example .env          # git-ignored; fill in keys for live mode
python -c "import secrets; print(secrets.token_urlsafe(48))"   # run twice:
#   one value for TITAN_SEAL_SECRET, one for TITAN_AUTH_SECRET

# 2. Backend
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 3. Web app (second terminal, from the repository root)
cd frontend
npm install
npm run dev                          # http://localhost:5173 (proxies /api to :8000)
```

Open http://localhost:5173/workspace, hold the mic (or Space) and say an offer.
Demo Login works in mock mode.

## Mock vs live

| `TITAN_MODE` | Behaviour |
| --- | --- |
| `mock` (default) | Offline and deterministic. Every output is labelled as demo. |
| `live` | Real Agnes, Sarvam, Magic Hour and Upload-Post calls. A missing key or provider failure is shown as an error — live mode never substitutes a mock. |

`GET /api/modes` reports exactly what is configured. Sarvam accepts up to four
keys (`TITAN_SARVAM_API_KEY`, `_2`, `_3`, `_4`); the next is used when one is
rejected, out of credits or rate limited.

## Deployment

| Piece | Where | Config |
| --- | --- | --- |
| API | Render (Docker) | `render.yaml` — provider keys are set in the dashboard |
| Website | Vercel | `frontend/vercel.json` — Root Directory: `frontend` |
| Android app | APK on the website's `/download` page | Drop the APK at `frontend/apps/web/public/downloads/svarah-ai.apk` |

## Documentation

- [Architecture](docs/architecture.md)
- [Setup and configuration](docs/setup.md)
- [Development guide](docs/development.md)
- [API](docs/api.md)
- [Security and deployment](docs/security-and-deployment.md)

## Tech stack

| Layer | Technology |
| --- | --- |
| Web | React 18, Vite, GSAP, Firebase Auth |
| Backend | Python, FastAPI, SQLAlchemy, Pillow, FFmpeg, Playwright |
| Language AI | Agnes 3.0 Flash |
| Images | Agnes image model |
| Video | Magic Hour, Agnes video |
| Speech | Sarvam Saaras (STT), Bulbul (TTS) |
| Publishing | Upload-Post |

## Security

Keys live in `.env` files (git-ignored) or the hosting dashboard, never in the
repository. See [docs/security-and-deployment.md](docs/security-and-deployment.md).
