"""Password hashing and stateless session tokens.

Identity is established here and nowhere else. Two rules hold throughout:

1. A client never supplies its own owner identifier. The owner UID is derived
   from a signed token that only the server can produce.
2. A missing signing key is never treated as "authentication disabled". Mock
   mode mints an unforgeable per-process key; live mode refuses to serve
   authentication at all (`Settings.auth_production_ready`).

Token layout (compact, stateless, URL-safe)::

    t1.<base64url(payload_json)>.<base64url(hmac_sha256(payload_segment))>

The payload carries ``uid``, ``email``, ``iat`` and ``exp``. There is no
server-side session store, so logout is a client-side token discard; shorten
``TITAN_AUTH_TOKEN_TTL_HOURS`` if that trade-off is not acceptable for a
deployment.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import AuthenticationError, AuthUnavailableError

TOKEN_VERSION = "t1"
_PBKDF2_ROUNDS = 390_000
_PBKDF2_PREFIX = "pbkdf2_sha256"

#: Minimum password length accepted at registration.
MIN_PASSWORD_LENGTH = 8


# ── password hashing ──────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256 with a per-password salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{_PBKDF2_PREFIX}${_PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    """Constant-time password check. A malformed stored hash verifies as False."""
    if not encoded:
        return False
    try:
        prefix, rounds_raw, salt_hex, digest_hex = encoded.split("$", 3)
        if prefix != _PBKDF2_PREFIX:
            return False
        rounds = int(rounds_raw)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(candidate, expected)


# ── stable owner UID ──────────────────────────────────────────────────
def owner_uid_for_email(email: str) -> str:
    """Deterministic, non-guessable-from-row-id owner UID for an account.

    Deriving the UID from the normalized email (rather than the database row
    id) keeps a UID stable across a database reset, which matters for local
    demos and makes test fixtures independent of insertion order.
    """
    digest = hashlib.sha256(normalize_email(email).encode("utf-8")).hexdigest()
    return f"u_{digest[:24]}"


def normalize_email(email: str) -> str:
    return email.strip().lower()


# ── tokens ────────────────────────────────────────────────────────────
def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _require_secret(settings: Settings) -> str:
    if not settings.auth_configured:
        raise AuthUnavailableError(
            "Authentication is not configured: set TITAN_AUTH_SECRET.",
            details={"configured": False},
        )
    if settings.titan_mode != "mock" and not settings.auth_production_ready:
        raise AuthUnavailableError(
            "Authentication is not configured for live mode: set a durable "
            "TITAN_AUTH_SECRET (an ephemeral key cannot be shared across restarts "
            "or replicas).",
            details={"configured": False, "ephemeral": True},
        )
    assert settings.auth_secret is not None  # narrowed by auth_configured
    return settings.auth_secret


def _sign(payload_segment: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256)
    return _b64encode(mac.digest())


def mint_token(*, uid: str, email: str, settings: Settings) -> tuple[str, int]:
    """Return ``(token, expires_at_epoch_seconds)``."""
    secret = _require_secret(settings)
    issued = int(time.time())
    expires = issued + max(1, settings.auth_token_ttl_hours) * 3600
    payload: dict[str, Any] = {"v": 1, "uid": uid, "email": email, "iat": issued, "exp": expires}
    segment = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return f"{TOKEN_VERSION}.{segment}.{_sign(segment, secret)}", expires


@dataclass(frozen=True)
class TokenClaims:
    uid: str
    email: str
    issued_at: int
    expires_at: int


def read_token(token: str, settings: Settings) -> TokenClaims:
    """Verify a token's signature and expiry, returning its claims.

    Raises ``AuthenticationError`` for anything unusable — a bad version, a
    tampered payload, a wrong signature or an expired token all produce the
    same client-visible failure so the response leaks no detail about which.
    """
    secret = _require_secret(settings)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_VERSION:
        raise AuthenticationError("The session token is malformed.", code="token_invalid")
    _, segment, signature = parts
    if not hmac.compare_digest(_sign(segment, secret), signature):
        raise AuthenticationError("The session token is not valid.", code="token_invalid")
    try:
        payload = json.loads(_b64decode(segment))
    except (ValueError, json.JSONDecodeError) as exc:
        raise AuthenticationError("The session token is malformed.", code="token_invalid") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("The session token is malformed.", code="token_invalid")
    uid = str(payload.get("uid") or "")
    email = str(payload.get("email") or "")
    try:
        issued_at = int(payload.get("iat") or 0)
        expires_at = int(payload.get("exp") or 0)
    except (TypeError, ValueError) as exc:
        raise AuthenticationError("The session token is malformed.", code="token_invalid") from exc
    if not uid:
        raise AuthenticationError("The session token is malformed.", code="token_invalid")
    if expires_at <= int(time.time()):
        raise AuthenticationError("The session has expired. Sign in again.", code="token_expired")
    return TokenClaims(uid=uid, email=email, issued_at=issued_at, expires_at=expires_at)


def bearer_token(header_value: str | None) -> str | None:
    """Extract the credential from an ``Authorization: Bearer <token>`` header."""
    if not header_value:
        return None
    scheme, _, value = header_value.partition(" ")
    if scheme.strip().lower() != "bearer":
        return None
    value = value.strip()
    return value or None


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "TOKEN_VERSION",
    "TokenClaims",
    "bearer_token",
    "hash_password",
    "mint_token",
    "normalize_email",
    "owner_uid_for_email",
    "read_token",
    "verify_password",
]
