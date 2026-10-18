"""Authentication: registration, sign-in, tokens, and the demo-login gate."""

from __future__ import annotations

import pytest


def test_me_requires_a_credential(client, anonymous):
    response = client.get("/api/auth/me", headers=anonymous)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


def test_register_then_use_the_returned_token(client, anonymous):
    response = client.post(
        "/api/auth/register",
        json={"email": "Shop.Owner@Example.com", "password": "correct horse battery", "display_name": "Shop Owner"},
        headers=anonymous,
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["token"]
    # The email is normalized and the owner UID is not a row id.
    assert session["account"]["email"] == "shop.owner@example.com"
    assert session["account"]["owner_uid"].startswith("u_")
    assert session["account"]["is_demo"] is False

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session['token']}"})
    assert me.status_code == 200
    assert me.json()["owner_uid"] == session["account"]["owner_uid"]


def test_register_rejects_a_duplicate_email(client, anonymous):
    payload = {"email": "dupe@example.com", "password": "a-long-enough-password"}
    assert client.post("/api/auth/register", json=payload, headers=anonymous).status_code == 201
    second = client.post("/api/auth/register", json=payload, headers=anonymous)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "email_taken"


def test_register_rejects_a_short_password(client, anonymous):
    response = client.post(
        "/api/auth/register",
        json={"email": "short@example.com", "password": "abc"},
        headers=anonymous,
    )
    assert response.status_code == 422


def test_register_rejects_a_malformed_email(client, anonymous):
    response = client.post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "a-long-enough-password"},
        headers=anonymous,
    )
    assert response.status_code == 422


def test_login_round_trip(client, anonymous):
    client.post(
        "/api/auth/register",
        json={"email": "login@example.com", "password": "a-long-enough-password"},
        headers=anonymous,
    )
    response = client.post(
        "/api/auth/login",
        json={"email": "login@example.com", "password": "a-long-enough-password"},
        headers=anonymous,
    )
    assert response.status_code == 200, response.text
    assert response.json()["account"]["email"] == "login@example.com"


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "login@example.com", "password": "wrong-password-entirely"},
        {"email": "never-registered@example.com", "password": "a-long-enough-password"},
    ],
)
def test_login_failures_are_indistinguishable(client, anonymous, payload):
    """A wrong password and an unknown address must look identical."""
    client.post(
        "/api/auth/register",
        json={"email": "login@example.com", "password": "a-long-enough-password"},
        headers=anonymous,
    )
    response = client.post("/api/auth/login", json=payload, headers=anonymous)
    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "invalid_credentials"
    assert "not valid" in body["message"]


def test_demo_login_works_in_mock_mode(client, anonymous):
    response = client.post("/api/auth/demo", headers=anonymous)
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["account"]["is_demo"] is True
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session['token']}"})
    assert me.status_code == 200


def test_demo_login_is_refused_when_disabled(client, anonymous, env_override):
    env_override(TITAN_ALLOW_DEMO_LOGIN="false")
    response = client.post("/api/auth/demo", headers=anonymous)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "demo_login_disabled"


def test_demo_login_is_refused_in_live_mode(client, anonymous, env_override):
    """Password-less sign-in must never be reachable outside mock mode."""
    env_override(TITAN_MODE="live")
    response = client.post("/api/auth/demo", headers=anonymous)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "demo_login_disabled"


# ── token handling ────────────────────────────────────────────────────
def test_a_tampered_token_is_rejected(client):
    from app.config import get_settings
    from app.security import mint_token, owner_uid_for_email

    token, _ = mint_token(
        uid=owner_uid_for_email("primary@titan.test"),
        email="primary@titan.test",
        settings=get_settings(),
    )
    version, payload, signature = token.split(".")
    # Flip one character of the signature.
    forged = f"{version}.{payload}.{signature[:-1]}{'A' if signature[-1] != 'A' else 'B'}"
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_invalid"


def test_an_expired_token_is_rejected(client, monkeypatch):
    import time as time_module

    from app import security
    from app.config import get_settings
    from app.security import mint_token, owner_uid_for_email

    token, expires = mint_token(
        uid=owner_uid_for_email("primary@titan.test"),
        email="primary@titan.test",
        settings=get_settings(),
    )
    monkeypatch.setattr(security.time, "time", lambda: expires + 10)
    response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "token_expired"
    assert time_module is not None  # keep the import meaningful to linters


def test_a_token_for_a_deleted_account_stops_working(client):
    """The token is stateless, but the account row is still checked."""
    from app.db import get_session_factory
    from app.models import User

    assert client.get("/api/auth/me").status_code == 200
    with get_session_factory()() as session:
        session.query(User).delete()
        session.commit()
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "account_missing"


def test_a_non_bearer_authorization_header_is_rejected(client):
    response = client.get("/api/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth_required"


def test_password_hashing_is_salted_and_verifies(client):
    from app.security import hash_password, verify_password

    first = hash_password("a-long-enough-password")
    second = hash_password("a-long-enough-password")
    assert first != second  # per-password salt
    assert verify_password("a-long-enough-password", first)
    assert not verify_password("a-long-enough-passwore", first)
    assert not verify_password("anything", None)
    assert not verify_password("anything", "not-a-valid-hash")


def test_modes_reports_auth_configuration_without_the_secret(client):
    body = client.get("/api/modes").json()
    assert body["auth"]["configured"] is True
    assert "detail" in body["auth"]
    # The secret itself must never appear anywhere in the payload.
    assert "test-auth-secret-do-not-use-in-prod" not in client.get("/api/modes").text
