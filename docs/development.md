# Svarah.ai — Marketing OS

Voice-first marketing system for small businesses with a **Fact Integrity Engine**:
AI creates the marketing, but code guarantees the facts. Every generated asset
(caption, poster, voice ad, reel) is verified against a locked, HMAC-sealed
FactSheet before its owner approves publication.

> **Architecture:** [`architecture.md`](architecture.md) · **Setup & config:** [`setup.md`](setup.md)
> **HTTP contract:** [`api.md`](api.md) · **Security:** [`security-and-deployment.md`](security-and-deployment.md)

## Feature map (Phases 1–9)

| Phase | Delivered |
| --- | --- |
| 1–2 | FastAPI backend, provider gateway (mock/verified slots), STT chain, versioned FactSheet, deterministic lock (`sha256` + HMAC seal), Fact Tokens |
| 3 | CampaignPlan from the locked tokens: channel copy (`instagram/facebook/x/whatsapp/poster/reel/voice`) + LocalizationSpecs (en-IN/hi-IN/kn-IN/ta-IN/te-IN) — token-only templates, deterministic substitution, persisted per locked FactSheet version |
| 4 | Poster compositor: Agnes-art slot (gated; labelled fallback art otherwise) + Pillow overlays **only from the token map** + rendered-fact registry + SHA-256 provenance |
| 5A/5B | Voice profiles (explicit consent required, owner-isolated, `voice_id` persisted and reused) + multilingual cloned-voice assets: chunked sentence-safe synthesis, base64 decode, WAV probe, checksums, mock labels |
| 6 | Reel generation: persisted job states + deterministic FFmpeg Ken-Burns compositor (terms: budget-capped, idempotent re-run), Agnes Video kept as gated enhancement |
| 7 | **Guardian**: numeric parity, unsourced numerics, unsupported claims, weekday parity, token residue, poster registry + OCR cross-check, ASR round-trip; bounded repair (≤2, then human review); Verification Certificate (JSON + HTML, HMAC-sealed, tamper-detecting); sabotage demo on copies only |
| 8 | Windsor.ai MCP publishing: real MCP `tools/call` client (mock in offline mode), connector/action discovery, schema validation, **explicit approval bound to caption+asset hashes**, sandbox publish, wa.me deep link, manual/export fallback for unsupported platforms (Facebook/X organic writes are correctly NOT assumed) |
| 9 | Docs, budgets, safety flags; dev/demo operates fully offline. Firebase production migration remains a **release blocker** (see `security-and-deployment.md`). |

## Quickstart (Windows PowerShell)

```powershell
# 1. Configuration (from the repository root)
Copy-Item .env.example .env        # then edit .env — it is git-ignored
python -c "import secrets; print(secrets.token_urlsafe(48))"   # run twice:
#   one value for TITAN_SEAL_SECRET (fact lock), one for TITAN_AUTH_SECRET.

# 2. Backend
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest                      # hermetic; never calls a provider
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000

# 3. Frontend (web) — in a second terminal, from the repository root
cd frontend
npm install
npm run dev      # http://localhost:5173  (proxies /api to :8000)
npm run build    # production build
```

Open http://localhost:5173 and sign in (**Demo Login** works in mock mode).

- **`/workspace`** — the voice-only thread (push to talk, no typing, no
  buttons in the flow):
  1. hold the mic and say the offer — the words appear live as you speak;
  2. the facts come back with their validation — hold the mic and say "yes",
     or say what to change ("make it 25 percent"), or "start over";
  3. on "yes" the package is made by itself: captions, a designed poster,
     voice-over, a campaign video and an AI video. The slow steps run side by
     side and the wait shows each stage live.

  Hold **Space** anywhere on the page (or hold/tap the mic) to talk.

  The poster and campaign video are art-directed by the **Brag Director**, a
  subagent on Agnes 3.0 Flash (`TITAN_BRAG_AGENT_MODEL`) that follows the
  brag-slim method (hook → reveal → highlights → outro). It chooses colours,
  type, layout and framing copy only; every fact is drawn from the locked
  tokens. Frames are rendered in headless Chrome/Edge via Playwright
  (`pip install -r requirements-optional.txt`) and encoded with FFmpeg. Without
  Playwright the poster falls back to the plain compositor.
- **`/studio`** — the full step-by-step studio for the same campaigns:
  capture → confirm facts (validation + live FactSheet JSON) → plan → create
  (posters, Sarvam speech, Agnes AI video) → verify → approve & export. The open
  campaign and step live in the URL, so a refresh returns to the same screen.

### Mock vs live

| `TITAN_MODE` | Behaviour |
| --- | --- |
| `mock` (default) | Fully offline and deterministic. Every output is labelled as demo: speech is a test tone, posters use placeholder art, the AI-video job stores an empty container. |
| `live` | Real calls: **Agnes** for fact extraction, semantic fact validation, the Campaign Director (plan + copy) and images; **Sarvam** for speech-to-text (including the live transcript while you speak) and text-to-speech; **Magic Hour** for AI video when `TITAN_MAGICHOUR_API_KEYS` is set (otherwise Agnes video). Needs `TITAN_AGNES_API_KEY`, `TITAN_SARVAM_API_KEY` and a durable `TITAN_AUTH_SECRET`. Demo Login is off in live mode: create an account with **Sign up**. A missing key or provider failure is shown as an error with a retry — live mode never substitutes a mock. |

`GET /api/modes` reports exactly what is configured. To check real provider
access (billable calls; reads the root `.env`, forces live mode for that process only):

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\smoke_live.py    # Agnes chat, Sarvam TTS → STT round trip, Agnes image
.\.venv\Scripts\python.exe scripts\smoke_live.py --video # also one 4-second Agnes video
```

The local poster-reel feature needs FFmpeg: either `ffmpeg`/`ffprobe` on `PATH`,
or the bundled binary from `pip install imageio-ffmpeg` (in
`requirements-optional.txt`). Without one that job fails with a clear reason
(AI video does not need FFmpeg).

## Golden path

```text
typed/voice input → Sarvam STT → Agnes extraction → schema + Agnes semantic validation
→ FactSheet edit/confirm → LOCK (hash+seal) → Agnes Campaign Director plan (token templates)
→ posters (Agnes image) + captions + speech (Sarvam TTS) + AI video (Agnes, async job)
→ Guardian verify → (bounded repair) → certificate → owner approval
→ sandbox publish / wa.me / manual export
```

## Engineering mantra

**Agents decide. Code guarantees. Facts are locked. Every transformation is
audited. Humans authorize publication.**
