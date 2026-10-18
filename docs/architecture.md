# Svarah.ai — Product Architecture

> **Create once. Publish everywhere. Facts remain trustworthy.**

Svarah.ai is a voice-first marketing workspace for small businesses. A shopkeeper
describes an offer by voice or text; the system extracts the *facts*, the owner
confirms and locks them, and only then does AI write copy, build media, verify it
against the locked facts, and prepare it for publication.

## The one architectural rule

**Facts are the source of truth; generated creative is not.** Every downstream
artifact is validated against an immutable, HMAC-sealed FactSheet before it can
be approved or published. This is enforced in code, not in the UI.

## Repository layout

| Role | Path |
| --- | --- |
| Backend API (FastAPI) | `backend/` |
| Web app (React + Vite) | `frontend/apps/web` |
| Docs | `docs/` |
| Render blueprint for the API | `render.yaml` |

## Backend

FastAPI application factory (`app/main.py`) mounting routers under `/api`:

```
app/
  main.py          app factory, lifespan, request-size middleware, CORS
  config.py         settings loader (.env, env vars, validation)
  db.py             SQLite/SQLAlchemy engine, create_all + additive migrations
  models.py         ORM: User, Shop, Campaign, FactSheetRecord, AssetRecord,
                    CampaignPlanRecord, JobRecord, VerificationResultRecord,
                    PublishRecord, VoiceProfile, AuditEvent
  schemas.py        API read/write contracts (response models)
  security.py       password hashing, signed session tokens, owner-uid derivation
  deps.py           current_user + ownership resolvers (owned_campaign/asset/…)
  errors.py         typed errors -> {error:{code,message,details}} envelope
  storage.py        filesystem asset store (path-safe reads)
  api/              health, auth, campaigns, factsheets, plans, assets,
                    voice_profiles, videos, guardian_api, publisher
  services/         fact engine, normalization, extraction, campaign brain,
                    assets, voice, video, STT, gateway, sarvam, agnes,
                    windsor, poster, ocr, asr, checksums, publisher_service
  guardian/         deterministic verification runner, text checks, certificate
```

### Fact Integrity Engine (do not casually refactor)

1. Input → transcription → deterministic **normalization** → FactSheet draft.
2. Owner edits via `PATCH /factsheets/{id}` (creates a new version if locked).
3. `lock` computes a canonical **SHA-256** over the facts and an **HMAC seal**
   (`TITAN_SEAL_SECRET`); the version becomes immutable.
4. The **campaign brain** builds copy from **fact tokens**, never literal values.
5. Trusted substitution injects only locked values into templates.
6. **Guardian** verifies each asset (numeric parity, day parity, token residue,
   unsupported claims, poster registry, OCR when available, ASR round-trip).
   A `FAIL` cannot be overridden; an inconclusive `NEEDS_REVIEW` can be accepted
   by an explicit, audited **human verification**.
7. Approval **binds** the caption hash and asset hashes; execution re-checks
   them, so a later edit invalidates the approval.

Seals: tampering with the facts or an invalid seal makes planning fail closed
(`facts_not_locked`). Verification failures block approval and publishing.

## Frontend

React 18 + Vite 6. Signature visual system preserved (Manrope/Georgia type,
DM Mono labels, lime accent, GSAP hero, WebGL ribbon-field shader).

```
apps/web/src/
  main.jsx           route table + marketing pages
  SvarahHero.jsx     animated hero (GSAP) — ex-FormeHero
  voice/
    VoicePill.jsx        press-and-hold mic: gesture, meter, slide-to-cancel
    useMicRecorder.js    MediaRecorder lifecycle + live input level
  workspace/
    VoiceWorkspace.jsx   the signed-in experience (see below)
  lib/api.js         the single typed API client (auth, timeout, errors)
  AuthContext.jsx    backend-backed session state
  Login.jsx / Signup.jsx
```

### The post-login experience

The shopkeeper never sees the pipeline. After signing in they get **a
microphone and a thread**:

1. Hold the mic and speak; the pill shows a live level meter, a timer and
   slide-to-cancel. Releasing uploads the clip for transcription.
2. The transcript appears as their message, followed by a plain-language
   summary of the extracted offer (business, product, discount, days, time,
   conditions) with **one** action: *Looks right — make my posts*.
   "Fix something" opens two fields for corrections.
3. Confirming runs the full integrity pipeline in the background — seal →
   plan → captions → poster → Guardian — reported as short progress chips.
   A human-behaviour failure is surfaced in words, not codes.
4. The finished poster, caption and a *Download* appear in the thread. When a
   check could not conclude by itself (for example OCR without Tesseract), the
   one tap offered is *Looks right to me*, which records an audited human
   decision — it never silently passes a failure.
5. Past offers live in the **History** rail.

The internal pipeline screens (fact form, plan table, asset list, publish
console) remain in `workspace/*Step.jsx` and are **no longer routed**; they are
kept as a reference implementation and are tree-shaken out of the bundle.

### Golden path

```
capture → extract facts → review → LOCK (hash + seal) → plan (token templates)
→ posters + captions + voice + reel → Guardian verify → (repair / human review)
→ certificate → explicit approval → export / sandbox publish / wa.me
```

The workspace reloads the affected resource from the backend after every
mutation; it keeps no local mirror of campaign data that could drift.

## Providers and modes

| Concern | Mock mode (default) | Live mode |
| --- | --- | --- |
| Extraction/planning/copy | deterministic token-safe templates (`is_mock`) | Agnes (`agnes-3.0-flash`) |
| Poster art | Pillow gradient fallback + registry | Agnes image |
| Video | FFmpeg Ken-Burns compositor | optional Agnes video |
| STT | labelled mock / typed input | Sarvam Saaras v4 → faster-whisper |
| Voice | labelled mock clone | Sarvam voice cloning |
| Publishing | offline sandbox + `wa.me` only | Windsor MCP, after approval |

Mock output is **always labelled**; nothing presents a mock as a live model run.
Switching modes never changes the fact-integrity rules.

## Related documents

- [`setup.md`](setup.md) — how to run it and every configuration variable.
- [`development.md`](development.md) — feature map, mock vs live, smoke tests.
- [`api.md`](api.md) — the HTTP contract.
- [`security-and-deployment.md`](security-and-deployment.md) — auth, limits, production gaps.
