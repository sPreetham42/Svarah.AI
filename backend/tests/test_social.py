"""Posting a campaign to the owner's own social accounts (Upload-Post)."""

from __future__ import annotations

import pytest

from app.api.social import quick_yes_no
from app.services.uploadpost import MockUploadPostClient

from .test_publisher import _setup, _verify_campaign, tesseract_present  # noqa: F401  (fixture)


@pytest.fixture(autouse=True)
def _no_linked_accounts():
    MockUploadPostClient._linked.clear()
    yield
    MockUploadPostClient._linked.clear()


@pytest.mark.parametrize("text", ["Yes", "Yes please, post it", "okay go ahead", "हाँ"])
def test_spoken_yes(text):
    assert quick_yes_no(text) == "yes"


@pytest.mark.parametrize("text", ["No", "No thanks", "not now", "No, I will post it myself", "नहीं", "ಬೇಡ"])
def test_spoken_no(text):
    assert quick_yes_no(text) == "no"


@pytest.mark.parametrize("text", ["Maybe on Tuesday at the cafe", "What does that cost?"])
def test_anything_else_is_not_a_quick_answer(text):
    assert quick_yes_no(text) is None


def test_status_starts_with_nothing_connected(client):
    body = client.get("/api/social/status").json()
    assert body["configured"] is True and body["is_mock"] is True
    assert [a["platform"] for a in body["accounts"]] == ["instagram", "facebook", "x"]
    assert not any(a["connected"] for a in body["accounts"])


def test_post_needs_a_connected_account(client, tesseract_present):
    campaign_id, _ = _setup(client)
    _verify_campaign(client, campaign_id)
    response = client.post(f"/api/campaigns/{campaign_id}/social/post")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "social_not_connected"


def test_connect_then_post_once(client, tesseract_present):
    campaign_id, poster_id = _setup(client)
    _verify_campaign(client, campaign_id)
    link = client.post("/api/social/connect", json={"redirect_url": "http://127.0.0.1:5173/workspace"}).json()
    assert link["access_url"] == "http://127.0.0.1:5173/workspace"
    assert [a["platform"] for a in client.get("/api/social/status").json()["accounts"] if a["connected"]] == ["instagram"]

    first = client.post(f"/api/campaigns/{campaign_id}/social/post")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["is_mock"] is True and body["asset_id"] == poster_id
    assert [(r["platform"], r["status"]) for r in body["results"]] == [("instagram", "PUBLISHED")]

    # Asking again never posts the same poster twice.
    again = client.post(f"/api/campaigns/{campaign_id}/social/post").json()
    assert again["results"][0]["already_posted"] is True
    assert again["results"][0]["publish_id"] == body["results"][0]["publish_id"]


def test_unverified_poster_is_never_posted(client):
    campaign_id, _ = _setup(client)  # not verified
    client.post("/api/social/connect", json={})
    response = client.post(f"/api/campaigns/{campaign_id}/social/post")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "asset_not_verified"


def test_social_routes_are_owner_scoped(client, other_account, tesseract_present):
    campaign_id, _ = _setup(client)
    assert client.post(f"/api/campaigns/{campaign_id}/social/post", headers=other_account).status_code == 404
    # One owner linking an account does not link it for anyone else.
    client.post("/api/social/connect", json={})
    other = client.get("/api/social/status", headers=other_account).json()
    assert not any(a["connected"] for a in other["accounts"])


def test_live_mode_without_a_key_reports_not_configured(client, env_override):
    env_override(TITAN_MODE="live", TITAN_AUTH_SECRET="test-auth-secret-do-not-use-in-prod")
    body = client.get("/api/social/status").json()
    assert body["configured"] is False
    assert client.post("/api/social/connect", json={}).json()["error"]["code"] == "social_not_configured"


def test_full_plan_reuses_an_empty_profile_slot(monkeypatch):
    """On a capped plan an unused (nothing linked) profile makes room; a linked one never does."""
    import httpx

    from app.config import get_settings
    from app.errors import ProviderUnavailableError
    from app.services import uploadpost

    profiles = {
        "default": {},
        "svarah-linked": {"instagram": {"username": "u1", "handle": "shop"}},
    }
    deleted: list[str] = []

    def fake_request(method, url, **kwargs):
        body = kwargs.get("json") or {}
        if method == "POST":
            if len(profiles) >= 2:
                return httpx.Response(403, json={"error_code": "PROFILE_LIMIT_REACHED"})
            profiles[body["username"]] = {}
            return httpx.Response(201, json={"success": True})
        if method == "GET":
            return httpx.Response(200, json={"profiles": [{"username": n, "social_accounts": a} for n, a in profiles.items()]})
        deleted.append(body["username"])
        del profiles[body["username"]]
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(uploadpost.httpx, "request", fake_request)
    client = uploadpost.UploadPostClient(get_settings())
    client.ensure_profile("svarah-new")
    assert deleted == ["default"] and "svarah-new" in profiles

    # Now both slots are in use by real accounts or the caller: nothing else is removed.
    profiles["svarah-new"] = {"x": {"username": "u2"}}
    with pytest.raises(ProviderUnavailableError) as failure:
        client.ensure_profile("svarah-third")
    assert failure.value.code == "social_profile_limit" and deleted == ["default"]
