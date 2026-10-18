# Svarah.ai — API Integration

Base URL: `/api` (dev: `http://localhost:8000/api`, proxied by Vite at
`http://localhost:5173/api`).

All owned routes require `Authorization: Bearer <token>` from `POST /api/auth/*`.
Every error uses one envelope:

```json
{ "error": { "code": "facts_not_locked", "message": "…", "details": null } }
```

`code` is stable and is what the UI branches on; `message` is for humans.

## System

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | status, version, mode, database — public |
| GET | `/modes` | provider/seal/auth/storage configuration report — public |

## Auth

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/auth/register` | email + password (≥8 chars) → session |
| POST | `/auth/login` | session; one message for unknown email *and* wrong password |
| POST | `/auth/demo` | password-less, mock mode only (`demo_login_disabled` otherwise) |
| GET | `/auth/me` | the authenticated account |

Session payload: `{token, expires_at, account:{owner_uid,email,display_name,is_demo}, ephemeral_signing_key}`.

## Campaign lifecycle

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/campaigns` | typed text **or** base64 audio → STT → extraction |
| GET | `/campaigns` | owner's campaigns (`?limit=`) |
| GET | `/campaigns/{id}` | facts, factsheet, versions, assets, verification, audit |
| POST | `/campaigns/{id}/extract` | re-extract (refused once locked) |
| POST | `/campaigns/{id}/transcript` | typed recovery when speech-to-text failed |
| POST | `/factsheets/{id}/validate` | deterministic rules + Agnes semantic validation → `factsheet.validation` |
| GET/PATCH | `/factsheets/{id}` | read / edit. **Editing a locked sheet supersedes it and opens a new draft version** |
| POST | `/factsheets/{id}/lock` | computes hash + HMAC seal; idempotent |
| POST/GET | `/campaigns/{id}/plan` | build/read the plan; `409 facts_not_locked` |
| POST | `/campaigns/{id}/assets/posters` | compose posters (registry + checksum) |
| POST | `/campaigns/{id}/assets/captions` | materialize substituted copy |
| GET | `/campaigns/{id}/assets` | assets + per-check verification rows |
| GET | `/campaigns/assets/{id}/file` | asset bytes (owner-checked; media-type allowlist) |

## Voice, video

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST/DELETE | `/voice-profiles[/{id}]` | consent required to create; owner-isolated |
| POST | `/campaigns/{id}/voice` | localized cloned-voice asset |
| GET | `/voice/options` | Sarvam TTS languages, speakers, speakable channels |
| POST | `/campaigns/{id}/tts` | speak plan copy with Sarvam TTS → voice asset |
| POST | `/campaigns/{id}/videos/generate` | submit an Agnes AI video task (202); poll `/videos/jobs` |
| POST | `/campaigns/videos/jobs/{job_id}/cancel` | stop tracking an active job |
| POST | `/campaigns/{id}/videos/jobs` | queue a reel job (202) |
| GET | `/campaigns/{id}/videos/jobs` | job states; polling advances active AI video jobs |
| POST | `/campaigns/videos/jobs/{job_id}/run` | drive a job to a terminal state |

## Guardian

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/campaigns/{id}/verify` | runs Guardian over every asset; returns verdicts |
| POST | `/assets/{id}/repair` | bounded repair (≤2 attempts) |
| POST | `/assets/{id}/human-verify` | **record an explicit owner decision on an inconclusive verdict** |
| POST | `/campaigns/{id}/certificate` | HMAC-sealed certificate |
| GET | `/campaigns/{id}/certificate/html` | human-readable certificate |
| POST | `/demo/sabotage/{asset_id}` | dev-only demo on a *copy* (requires the flag + mock mode) |

`human-verify` contract: body `{attestation: true, note?}`. Guardian must have
run (`409 verification_required`); the attestation must be explicit
(`422 attestation_required`); a hard `FAIL` is refused (`409 human_verify_refused`).
The **latest** aggregate verdict is authoritative, so a repaired asset is not
blocked by a superseded failure. Guardian rows are preserved.

## Publishing and export

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/campaigns/{id}/publish/capabilities` | connectors/actions discovered (no live writes) |
| POST | `/campaigns/{id}/publish/prepare` | binds asset + caption hashes; only `verified`/`human_verified` assets (`409 asset_not_verified`) |
| POST | `/campaigns/publish/{id}/approve` | explicit owner approval |
| POST | `/campaigns/publish/{id}/execute` | re-checks eligibility (`409 approval_stale`); mock mode is sandbox-only |
| GET | `/campaigns/{id}/publish/records` | prepared/approved/executed records |
| GET | `/campaigns/{id}/publish/export` | JSON bundle (captions, assets, records, wa.me deep link) |

## Frontend client

Everything the web app sends goes through `apps/web/src/lib/api.js`:

- relative URLs by default (Vite proxy), configurable via `VITE_API_BASE`;
- bearer token attached from the stored session (`svarah.session.v1`);
- one `ApiError` shape carrying `code`/`status`, plus `isAuthFailure`/`isOffline`;
- 20 s default timeout, 60–180 s for generation calls, `AbortSignal` passthrough;
- blob helper for owner-guarded asset bytes (`fetchAssetObjectUrl`), because a
  plain `<img src>` cannot carry the bearer token;
- empty/missing bodies normalized into an explicit `bad_response` error.

The workspace never infers success from HTTP 200 alone: the *verdict in the
body* is what the Verify step renders, and approval/publish buttons are gated on
the backend's own eligibility checks.

## Mock vs live

`GET /modes` is the honest source of truth. In mock mode every provider reports
its mock status and generated media carries `is_mock`/`used_fallback`, so the UI
labels demo output. Live providers are used only when their credentials are
configured; a failure surfaces as an error and never as a fake success.
