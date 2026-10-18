"""Firebase sign-in: turn a Firebase ID token into a verified phone number or Google account.

The browser proves ownership of a phone number (SMS code) or a Google account
(Google's own sign-in window) to Firebase and receives a Firebase ID token. That token is *not* trusted here: it is handed to
Google's Identity Toolkit, which checks its signature, expiry and project and
returns the account it belongs to::

    POST https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=<web api key>
         {"idToken": "<token>"}
      →  {"users": [{"localId": "...", "phoneNumber": "+9198…", "disabled": false}]}

Asking Google rather than verifying the JWT locally keeps the backend free of
an RSA dependency, and also rejects accounts that were disabled or deleted
after the token was issued. A token minted for any other Firebase project is
refused, because the lookup is scoped to the project the API key belongs to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..errors import AuthenticationError, AuthUnavailableError

LOOKUP_URL = "https://identitytoolkit.googleapis.com/v1/accounts:lookup"
_TIMEOUT_SECONDS = 15.0
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")

#: Identity Toolkit error codes that mean "this token is no good" (as opposed
#: to "the backend is misconfigured").
_BAD_TOKEN_CODES = ("INVALID_ID_TOKEN", "TOKEN_EXPIRED", "USER_NOT_FOUND", "USER_DISABLED", "CREDENTIAL_TOO_OLD")


@dataclass(frozen=True)
class FirebaseIdentity:
    uid: str
    phone: str


@dataclass(frozen=True)
class GoogleIdentity:
    uid: str
    email: str
    name: str


def _lookup_account(id_token: str, settings: Settings) -> dict:
    """Ask Google which Firebase account an ID token belongs to.

    Raises ``AuthUnavailableError`` when Firebase sign-in is not configured or
    Google cannot be reached, and ``AuthenticationError`` for a token that is
    invalid, expired, from another project, or for a disabled account.
    """
    if not settings.firebase_api_key:
        raise AuthUnavailableError(
            "Sign-in is not configured: set TITAN_FIREBASE_API_KEY.",
            code="phone_login_unavailable",
            details={"configured": False},
        )
    try:
        response = httpx.post(
            LOOKUP_URL,
            params={"key": settings.firebase_api_key},
            json={"idToken": id_token},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise AuthUnavailableError(
            "The sign-in service could not be reached. Try again in a moment.",
            code="phone_login_unreachable",
        ) from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        message = str((body.get("error") or {}).get("message") or "") if isinstance(body, dict) else ""
        if any(code in message for code in _BAD_TOKEN_CODES):
            raise AuthenticationError(
                "That sign-in could not be verified. Please try again.",
                code="phone_token_invalid",
            )
        # Anything else (a bad API key, the API disabled) is ours to fix, not the caller's.
        raise AuthUnavailableError(
            "Sign-in is misconfigured on the server.",
            code="phone_login_unavailable",
            details={"status": response.status_code},
        )

    users = body.get("users") if isinstance(body, dict) else None
    account = users[0] if isinstance(users, list) and users and isinstance(users[0], dict) else None
    uid = str((account or {}).get("localId") or "")
    if not uid or (account or {}).get("disabled"):
        raise AuthenticationError(
            "That sign-in could not be verified. Please try again.",
            code="phone_token_invalid",
        )
    return account


def verify_phone_id_token(id_token: str, settings: Settings) -> FirebaseIdentity:
    """Resolve a Firebase ID token to the phone number Firebase verified."""
    account = _lookup_account(id_token, settings)
    phone = str(account.get("phoneNumber") or "")
    if not _E164.match(phone):
        # A valid Firebase account, but not one that proved a phone number
        # (for example a Google sign-in from the same project).
        raise AuthenticationError(
            "This sign-in did not verify a phone number.", code="phone_not_verified"
        )
    return FirebaseIdentity(uid=str(account["localId"]), phone=phone)


def verify_google_id_token(id_token: str, settings: Settings) -> GoogleIdentity:
    """Resolve a Firebase ID token to the Google account that signed in."""
    account = _lookup_account(id_token, settings)
    providers = account.get("providerUserInfo")
    google = next(
        (
            p
            for p in (providers if isinstance(providers, list) else [])
            if isinstance(p, dict) and p.get("providerId") == "google.com"
        ),
        None,
    )
    email = str((google or {}).get("email") or "").strip().lower()
    if google is None or not email or not account.get("emailVerified"):
        # A valid Firebase account, but not one that signed in with Google.
        raise AuthenticationError(
            "This sign-in did not come from a Google account.", code="google_not_verified"
        )
    name = str(google.get("displayName") or account.get("displayName") or "").strip()
    return GoogleIdentity(uid=str(account["localId"]), email=email, name=name)
