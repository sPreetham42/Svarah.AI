"""GitHub OAuth (web flow) without Firebase: a one-time `code` becomes a verified GitHub account.

The browser sends the visitor to GitHub, which redirects back with `?code=`.
The backend trades that code (plus the client secret) for an access token, then
reads the account and its primary *verified* email. The token is discarded.
"""

from __future__ import annotations

import httpx

from ..config import Settings
from ..errors import AuthenticationError, AuthUnavailableError
from .firebase_auth import GoogleIdentity

_TIMEOUT = 15.0
_HEADERS = {"Accept": "application/json", "User-Agent": "svarah-ai"}


def verify_github_code(code: str, settings: Settings) -> GoogleIdentity:
    if not settings.github_client_id or not settings.github_client_secret:
        raise AuthUnavailableError(
            "GitHub sign-in is not configured: set TITAN_GITHUB_CLIENT_ID and TITAN_GITHUB_CLIENT_SECRET.",
            code="github_login_unavailable",
        )
    bad = AuthenticationError("That GitHub sign-in could not be verified. Please try again.", code="github_code_invalid")
    try:
        token_resp = httpx.post(
            "https://github.com/login/oauth/access_token",
            headers=_HEADERS,
            data={"client_id": settings.github_client_id, "client_secret": settings.github_client_secret, "code": code},
            timeout=_TIMEOUT,
        )
        token = (token_resp.json() or {}).get("access_token")
        if not token:
            raise bad
        auth = {**_HEADERS, "Authorization": f"Bearer {token}"}
        user = httpx.get("https://api.github.com/user", headers=auth, timeout=_TIMEOUT).json()
        emails = httpx.get("https://api.github.com/user/emails", headers=auth, timeout=_TIMEOUT).json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AuthUnavailableError(
            "GitHub could not be reached. Try again in a moment.", code="github_login_unreachable"
        ) from exc
    email = next(
        (str(e.get("email") or "").lower() for e in emails if isinstance(e, dict) and e.get("primary") and e.get("verified")),
        "",
    ) if isinstance(emails, list) else ""
    if not isinstance(user, dict) or not user.get("id") or not email:
        raise AuthenticationError(
            "This GitHub account has no verified primary email.", code="github_not_verified"
        )
    return GoogleIdentity(uid=f"gh_{user['id']}", email=email, name=str(user.get("name") or user.get("login") or ""))
