"""Phone sign-in: a Firebase-verified number exchanged for a backend session."""

from __future__ import annotations

import httpx

FAKE_ID_TOKEN = "firebase-id-token-" + "x" * 40


def _fake_lookup(monkeypatch, *, status=200, body=None, error=None):
    """Replace the Identity Toolkit call with a canned reply; returns the call log."""
    from app.services import firebase_auth

    calls = []

    def _post(url, *, params, json, timeout):
        calls.append({"url": url, "params": params, "json": json})
        if error is not None:
            raise error
        return httpx.Response(status, json=body or {})

    monkeypatch.setattr(firebase_auth.httpx, "post", _post)
    return calls


def _sign_in(client, anonymous):
    return client.post("/api/auth/phone", json={"id_token": FAKE_ID_TOKEN}, headers=anonymous)


def test_unavailable_until_firebase_is_configured(client, anonymous, env_override, monkeypatch):
    monkeypatch.delenv("TITAN_FIREBASE_API_KEY", raising=False)
    env_override()
    response = _sign_in(client, anonymous)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "phone_login_unavailable"
    assert client.get("/api/modes").json()["auth"]["phone_login_enabled"] is False


def test_creates_then_reuses_the_account(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    calls = _fake_lookup(
        monkeypatch, body={"users": [{"localId": "fbUid123", "phoneNumber": "+919876543210"}]}
    )
    assert client.get("/api/modes").json()["auth"]["phone_login_enabled"] is True

    first = _sign_in(client, anonymous)
    assert first.status_code == 200, first.text
    account = first.json()["account"]
    assert account["phone"] == "+919876543210"
    assert account["owner_uid"] == "fb_fbUid123"
    assert account["display_name"] == "+919876543210"
    # The token went to Google with the project's key, and nowhere else.
    assert calls[0]["json"] == {"idToken": FAKE_ID_TOKEN}
    assert calls[0]["params"] == {"key": "web-api-key"}

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {first.json()['token']}"})
    assert me.status_code == 200
    assert me.json()["phone"] == "+919876543210"

    second = _sign_in(client, anonymous)
    assert second.status_code == 200
    assert second.json()["account"]["owner_uid"] == account["owner_uid"]


def test_a_session_can_be_renewed(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, body={"users": [{"localId": "fbUid123", "phoneNumber": "+919876543210"}]})
    first = _sign_in(client, anonymous).json()

    renewed = client.post("/api/auth/refresh", headers={"Authorization": f"Bearer {first['token']}"})
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["account"] == first["account"]
    assert renewed.json()["expires_at"] >= first["expires_at"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {renewed.json()['token']}"})
    assert me.status_code == 200

    assert client.post("/api/auth/refresh", headers=anonymous).status_code == 401


def test_rejects_a_token_google_does_not_accept(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, status=400, body={"error": {"code": 400, "message": "INVALID_ID_TOKEN"}})
    response = _sign_in(client, anonymous)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "phone_token_invalid"


def test_rejects_an_account_without_a_verified_phone(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, body={"users": [{"localId": "emailOnly", "email": "a@b.co"}]})
    response = _sign_in(client, anonymous)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "phone_not_verified"


def test_rejects_a_disabled_account(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(
        monkeypatch,
        body={"users": [{"localId": "gone", "phoneNumber": "+919876500000", "disabled": True}]},
    )
    assert _sign_in(client, anonymous).status_code == 401


def test_a_bad_server_key_is_reported_as_unavailable(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="wrong-key")
    _fake_lookup(monkeypatch, status=400, body={"error": {"message": "API key not valid."}})
    response = _sign_in(client, anonymous)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "phone_login_unavailable"


def test_an_unreachable_google_is_reported_as_unavailable(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, error=httpx.ConnectError("no route"))
    response = _sign_in(client, anonymous)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "phone_login_unreachable"


def test_register_refuses_the_reserved_placeholder_domain(client, anonymous):
    response = client.post(
        "/api/auth/register",
        json={"email": "919876543210@phone.svarah.invalid", "password": "a-long-enough-password"},
        headers=anonymous,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "email_reserved"


# ── Google sign-in (same Firebase project) ─────────────────────────────
def _google_account(email="owner@gmail.com", **extra):
    return {
        "users": [
            {
                "localId": "fbGoogle1",
                "email": email,
                "emailVerified": True,
                "providerUserInfo": [
                    {"providerId": "google.com", "email": email, "displayName": "Asha Rao"}
                ],
                **extra,
            }
        ]
    }


def _google_sign_in(client, anonymous):
    return client.post("/api/auth/google", json={"id_token": FAKE_ID_TOKEN}, headers=anonymous)


def test_google_creates_then_reuses_the_account(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, body=_google_account())
    assert client.get("/api/modes").json()["auth"]["google_login_enabled"] is True

    first = _google_sign_in(client, anonymous)
    assert first.status_code == 200, first.text
    account = first.json()["account"]
    assert account["email"] == "owner@gmail.com"
    assert account["display_name"] == "Asha Rao"
    assert account["owner_uid"] == "fb_fbGoogle1"
    assert account["phone"] is None

    second = _google_sign_in(client, anonymous)
    assert second.json()["account"]["owner_uid"] == account["owner_uid"]


def test_google_reuses_an_existing_email_account(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    registered = client.post(
        "/api/auth/register",
        json={"email": "Owner@Gmail.com", "password": "correct horse battery"},
        headers=anonymous,
    )
    assert registered.status_code == 201, registered.text
    _fake_lookup(monkeypatch, body=_google_account())
    response = _google_sign_in(client, anonymous)
    assert response.status_code == 200, response.text
    assert response.json()["account"]["owner_uid"] == registered.json()["account"]["owner_uid"]


def test_google_rejects_a_sign_in_that_is_not_google(client, anonymous, env_override, monkeypatch):
    env_override(TITAN_FIREBASE_API_KEY="web-api-key")
    _fake_lookup(monkeypatch, body={"users": [{"localId": "fbUid123", "phoneNumber": "+919876543210"}]})
    response = _google_sign_in(client, anonymous)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "google_not_verified"
