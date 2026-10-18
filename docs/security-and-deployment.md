# Svarah.ai — Security and Deployment

## Identity

Session identity is issued **only** by the backend (`app/security.py`):

- passwords: PBKDF2-HMAC-SHA256, 390 000 iterations, per-password salt.
- tokens: `t1.<base64url(payload)>.<base64url(hmac_sha256)>` over
  `{v,uid,email,iat,exp}`, signed with `TITAN_AUTH_SECRET`.
- `owner_uid` is derived from the normalized email (`u_<sha256[..24]>`), so it is
  stable across a database reset and is not guessable from a row id.

**A client never supplies its own owner identity.** Routers previously accepted
`owner_uid` in the body/query; that trust has been removed and identity now comes
exclusively from the verified token. Mock mode mints an unforgeable per-process
key when no secret is configured; live mode refuses to serve authentication at
all without a durable `TITAN_AUTH_SECRET` (fail closed).

## Ownership

`app/deps.py` resolves every owned resource by id **and** owner:

- `current_user` — validates the token, then loads the live account row (a
  deleted account stops working even while its token is unexpired).
- `owned_campaign` / `owned_asset` / `owned_factsheet` / `owned_publish_record`.
- A resource owned by someone else returns **404, not 403**, so probing ids
  cannot reveal which records exist.

Ownership is enforced on campaigns, fact sheets, plans, assets (including the
byte-download route), voice profiles, video jobs, certificates, and publish
records. Responses do not leak `owner_uid`.

## Input and transport hardening

| Control | Where |
| --- | --- |
| Request-size ceiling before parsing | `RequestSizeLimitMiddleware` (`TITAN_MAX_REQUEST_BYTES`) |
| CORS restricted to configured origins | `app/main.py` |
| Asset bytes served with a media-type allowlist; unknown types are opaque downloads | `api/assets.py` |
| Path-safe asset reads (storage root confinement) | `services/storage.py` |
| Bounded provider retries/timeouts | `config.py`, `services/gateway.py` |
| Safe subprocess argument construction for FFmpeg | `services/video_service.py` |
| Secrets never returned in responses or logs | `config.py`, `api/health.py` (db URL sanitized) |
| `.env` ignored by Git (`.env.example` tracked) | `.gitignore` |

## Publication safety

- Mock mode can **never** write to a social platform; only the offline sandbox
  destination and the `wa.me` deep link are available.
- Preparing a publication binds the caption hash and asset hashes.
- Only `verified` or `human_verified` assets are acceptable
  (`409 asset_not_verified`); superseded facts give `asset_stale`.
- Execution re-checks eligibility (`409 approval_stale`), so content edited
  after approval invalidates the approval.
- Windsor publishing stays disabled until explicitly configured, and every
  external publish requires explicit owner approval.
- A successful HTTP response is never reported as a successful post: the record
  status and destination come from the backend.

## Verification integrity

- Guardian `FAIL` cannot be overridden by the human-decision route.
- `human-verify` requires an explicit attestation, is owner-scoped, and is
  audited (`asset.human_verified`, recording the preceding status, the Guardian
  verdict, and the reviewer's note).
- Guardian result rows are preserved, so certificates and audit still report
  what the machine found alongside the human decision.
- The **latest** aggregate verdict is authoritative, so a repaired asset is not
  permanently blocked by a superseded failure.

## Known limitations (honest, not hidden)

- Session tokens are stateless: there is no server-side revocation list, so
  logout is a client-side token discard. Shorten `TITAN_AUTH_TOKEN_TTL_HOURS` if
  that trade-off is unacceptable.
- No password reset or email verification flow.
- The demo sign-in is a mock-mode convenience, not a security model.
- Owner isolation relies on the signed token; there is no multi-tenant
  organization layer.
- OCR verification needs Tesseract; without it, that check reports
  `NEEDS_REVIEW` (disclosed) and publication requires a recorded human decision.
- The certificate seal is symmetric HMAC — tamper-evident, not a public-key
  signature.

## Production requirements (before real users)

1. **Durable auth secret** and a real identity provider (or hardened local auth),
   with password reset and email verification.
2. **Managed database** (Postgres) with backups, migrations tooling, and
   point-in-time recovery; the schema is already Postgres-compatible.
3. **TLS termination**, HSTS, and a strict production CORS origin list.
4. **Token revocation / session store** and rate limiting on `/auth/*`.
5. **Object storage** for assets with signed, time-limited URLs instead of
   streaming bytes through the app.
6. **Secret management** (a vault or platform secret store), never `.env` files
   in the image.
7. **Provider credentials + interface verification** recorded per provider
   before live use (`GET /api/modes` reports what is configured).
8. **Observability**: structured logs, request tracing, error alerting, and audit
   retention policy.
9. A **data-retention and deletion** policy for voice samples and generated media.

Until these are addressed, this is a **complete, working local application with a
clear path to deployment** — not a production-ready multi-tenant service.
