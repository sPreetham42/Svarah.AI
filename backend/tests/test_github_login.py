"""GitHub OAuth sign-in: a one-time code exchanged for a backend session (no Firebase)."""

from __future__ import annotations

import httpx

ENV = dict(TITAN_GITHUB_CLIENT_ID="cid", TITAN_GITHUB_CLIENT_SECRET="secret")


def _fake_github(monkeypatch, *, token="gho_x", emails=None):
    from app.services import github_auth

    def post(url, **kw):
        return httpx.Response(200, json={"access_token": token} if token else {"error": "bad_verification_code"})

    def get(url, **kw):
        if url.endswith("/user"):
            return httpx.Response(200, json={"id": 42, "login": "dev", "name": "Dev"})
        return httpx.Response(200, json=emails if emails is not None else [{"email": "Dev@Example.com", "primary": True, "verified": True}])

    monkeypatch.setattr(github_auth.httpx, "post", post)
    monkeypatch.setattr(github_auth.httpx, "get", get)


def _sign_in(client, anonymous):
    return client.post("/api/auth/github", json={"code": "abc123"}, headers=anonymous)


def test_unavailable_until_configured(client, anonymous, env_override, monkeypatch):
    monkeypatch.delenv("TITAN_GITHUB_CLIENT_ID", raising=False)
    env_override()
    assert _sign_in(client, anonymous).status_code == 503
    auth = client.get("/api/modes").json()["auth"]
    assert auth["github_login_enabled"] is False and auth["github_client_id"] is None


def test_signs_in_with_the_verified_primary_email(client, anonymous, env_override, monkeypatch):
    env_override(**ENV)
    _fake_github(monkeypatch)
    auth = client.get("/api/modes").json()["auth"]
    assert auth["github_login_enabled"] is True and auth["github_client_id"] == "cid"
    response = _sign_in(client, anonymous)
    assert response.status_code == 200, response.text
    assert response.json()["account"]["email"] == "dev@example.com"


def test_refuses_a_bad_code_and_an_unverified_email(client, anonymous, env_override, monkeypatch):
    env_override(**ENV)
    _fake_github(monkeypatch, token=None)
    assert _sign_in(client, anonymous).status_code == 401
    _fake_github(monkeypatch, emails=[{"email": "a@b.co", "primary": True, "verified": False}])
    assert _sign_in(client, anonymous).status_code == 401
