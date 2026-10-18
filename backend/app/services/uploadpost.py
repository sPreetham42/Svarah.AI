"""Upload-Post client: post to the owner's OWN social accounts.

Each Svarah account maps to one Upload-Post *profile*. The owner links their
Instagram / Facebook Page / X on Upload-Post's hosted connect page (they sign
in to the social network there — Svarah never sees a social password), and a
post is then made with the campaign's verified poster and caption.

- auth: ``Authorization: Apikey <TITAN_UPLOADPOST_API_KEY>``;
- profiles: ``POST /api/uploadposts/users``, ``GET /api/uploadposts/users/{username}``;
- connect link: ``POST /api/uploadposts/users/generate-jwt`` → ``access_url`` (48 h);
- photo post: ``POST /api/upload_photos`` (multipart: ``user``, ``platform[]``,
  ``photos[]``, ``title``) → per-platform ``results``.

Mock mode never calls the network: the mock client links a labelled demo
account and returns labelled mock post ids. In live mode a missing key is an
error, never a silent mock.
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError

#: Networks a poster (an image post) can be sent to.
PLATFORMS = ("instagram", "facebook", "x")


class UploadPostClient:
    """Minimal synchronous client for the endpoints Svarah uses."""

    is_mock = False

    def __init__(self, settings: Settings) -> None:
        self._base = settings.uploadpost_api_base.rstrip("/")
        self._key = settings.uploadpost_api_key or ""

    def _request(self, method: str, path: str, *, ok: tuple[int, ...] = (), timeout: float = 30.0, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Apikey {self._key}", **kwargs.pop("headers", {})}
        try:
            response = httpx.request(method, f"{self._base}{path}", headers=headers, timeout=timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"The posting service could not be reached: {exc}", code="social_unreachable"
            ) from exc
        if response.status_code == 401:
            raise ProviderUnavailableError(
                "The posting service rejected Svarah's API key.", code="social_auth_failed"
            )
        if response.status_code >= 400 and response.status_code not in ok:
            raise ProviderUnavailableError(
                _error_text(response) or f"The posting service returned an error ({response.status_code}).",
                code="social_limit_reached" if response.status_code in (403, 429) else "social_error",
                details={"status": response.status_code},
            )
        return response

    def ensure_profile(self, username: str) -> None:
        """Create the profile if it does not exist yet (409 = already there)."""
        response = self._request("POST", "/api/uploadposts/users", json={"username": username}, ok=(403, 409))
        if response.status_code == 403 and self._free_empty_profile(keep=username):
            response = self._request("POST", "/api/uploadposts/users", json={"username": username}, ok=(403, 409))
        if response.status_code == 403:
            # Every Svarah account needs its own profile and the plan caps them.
            raise ProviderUnavailableError(
                "Svarah's posting plan has no room for another account, so yours could not be "
                "connected. Nothing was posted — ask the Svarah team to free a slot or upgrade.",
                code="social_profile_limit",
            )

    def _free_empty_profile(self, *, keep: str) -> bool:
        """Make room on a full plan by removing one profile that links nothing.

        A profile with no social account holds no data, so the oldest such one
        is the slot to reuse. Profiles with a linked account are never touched.
        """
        profiles = _json(self._request("GET", "/api/uploadposts/users")).get("profiles") or []
        for profile in profiles:  # the provider lists oldest first
            name = profile.get("username") if isinstance(profile, dict) else None
            if not name or name == keep or _linked(profile.get("social_accounts")):
                continue
            self._request("DELETE", "/api/uploadposts/users", json={"username": name})
            return True
        return False

    def accounts(self, username: str) -> dict[str, dict[str, Any]]:
        """Connected accounts by platform; empty when the profile does not exist."""
        response = self._request("GET", f"/api/uploadposts/users/{username}", ok=(404,))
        if response.status_code == 404:
            return {}
        return _linked((_json(response).get("profile") or {}).get("social_accounts"))


    def connect_url(self, username: str, *, redirect_url: str | None, platforms: tuple[str, ...]) -> str:
        body: dict[str, Any] = {
            "username": username,
            "platforms": list(platforms),
            "connect_title": "Connect your social accounts to Svarah.AI",
            "connect_description": "Sign in to the accounts you want Svarah.AI to post your campaign to.",
            "redirect_button_text": "Back to Svarah.AI",
            "show_calendar": False,
        }
        if redirect_url:
            body["redirect_url"] = redirect_url
        url = _json(self._request("POST", "/api/uploadposts/users/generate-jwt", json=body)).get("access_url")
        if not url:
            raise ProviderUnavailableError("The posting service did not return a connect link.", code="social_error")
        return str(url)

    def upload_photo(
        self,
        username: str,
        *,
        platforms: list[str],
        caption: str,
        filename: str,
        data: bytes,
        mime: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Publish one image post. Returns the provider's raw JSON body."""
        fields: list[tuple[str, str]] = [("user", username), ("title", caption), ("description", caption)]
        fields += [("platform[]", platform) for platform in platforms]
        response = self._request(
            "POST",
            "/api/upload_photos",
            data=fields,
            files=[("photos[]", (filename, data, mime))],
            headers={"Idempotency-Key": idempotency_key},
            timeout=120.0,
        )
        return _json(response)


def _linked(raw: Any) -> dict[str, dict[str, Any]]:
    """The platforms of a profile's ``social_accounts`` that are really linked."""
    connected: dict[str, dict[str, Any]] = {}
    for platform, value in (raw if isinstance(raw, dict) else {}).items():
        # An unlinked network is "", null or a bare placeholder.
        if not isinstance(value, dict) or not (value.get("username") or value.get("handle") or value.get("display_name")):
            continue
        connected[platform] = {
            "handle": value.get("handle") or value.get("display_name") or value.get("username") or "",
            "reauth_required": bool(value.get("reauth_required")),
        }
    return connected


class MockUploadPostClient:
    """Offline stand-in: no social network is ever contacted."""

    is_mock = True
    #: profile username → linked platforms (per process).
    _linked: dict[str, dict[str, dict[str, Any]]] = {}

    def ensure_profile(self, username: str) -> None:
        self._linked.setdefault(username, {})

    def accounts(self, username: str) -> dict[str, dict[str, Any]]:
        return dict(self._linked.get(username, {}))

    def connect_url(self, username: str, *, redirect_url: str | None, platforms: tuple[str, ...]) -> str:
        # There is no real sign-in to do offline: link a labelled demo account
        # and send the owner straight back.
        self._linked.setdefault(username, {})["instagram"] = {"handle": "demo_shop (demo)", "reauth_required": False}
        return redirect_url or "/workspace"

    def upload_photo(self, username: str, *, platforms: list[str], caption: str, filename: str, data: bytes, mime: str, idempotency_key: str) -> dict[str, Any]:
        digest = hashlib.sha1(f"{username}:{idempotency_key}".encode()).hexdigest()[:12]
        linked = self._linked.get(username, {})
        return {
            "success": True,
            "results": {
                platform: ({"success": True, "post_id": f"mock-post-{digest}"} if platform in linked else {"skipped": True})
                for platform in platforms
            },
        }


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _error_text(response: httpx.Response) -> str:
    body = _json(response)
    return str(body.get("message") or body.get("error") or "")[:300]


def get_social_client(settings: Settings | None = None) -> Any:
    """Mock client in mock mode; the real client in live mode (key required)."""
    settings = settings or get_settings()
    if settings.is_mock:
        return MockUploadPostClient()
    if not settings.uploadpost_configured:
        raise ProviderUnavailableError(
            "Posting to social media is not set up on this server (TITAN_UPLOADPOST_API_KEY).",
            code="social_not_configured",
        )
    return UploadPostClient(settings)


__all__ = ["PLATFORMS", "UploadPostClient", "MockUploadPostClient", "get_social_client"]
