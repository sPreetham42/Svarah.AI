"""Phase 8 publisher tests: discovery, VERIFIED-only gating, fallbacks, mock safety."""

from __future__ import annotations

import pytest


def _setup(client) -> tuple[int, int]:
    """Returns (campaign_id, poster_asset_id) with captions materialized."""
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.patch(f"/api/factsheets/{sheet_id}", json={"languages": ["English", "Hindi"]})
    client.post(f"/api/factsheets/{sheet_id}/lock")
    client.post(f"/api/campaigns/{campaign_id}/plan")
    assets = client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1}).json()
    client.post(f"/api/campaigns/{campaign_id}/assets/captions")
    return campaign_id, assets[0]["id"]


@pytest.fixture
def tesseract_present(monkeypatch):
    """Make the Guardian's OCR leg runnable so posters verify in this environment.

    Posters are NEEDS_REVIEW wherever Tesseract is unavailable (disclosed, by
    design). Stubbing the OCR helper lets the publisher success paths run
    against genuinely VERIFIED posters instead of forcing asset statuses.
    """
    import shutil as _shutil

    from app.guardian.checks_text import CheckOutcome
    from app.services import ocr

    monkeypatch.setattr(
        _shutil, "which", lambda name: "/usr/bin/tesseract" if name == "tesseract" else None
    )
    monkeypatch.setattr(
        ocr,
        "ocr_fact_zone",
        lambda data, expected=None: CheckOutcome(
            "ocr_cross_check", "PASS", 1.0, {"note": "OCR stubbed for this test."}
        ),
    )


def _verify_campaign(client, campaign_id: int) -> list[dict]:
    response = client.post(f"/api/campaigns/{campaign_id}/verify")
    assert response.status_code == 200, response.text
    return response.json()


def _caption_asset(client, campaign_id: int) -> dict:
    captions = [
        a
        for a in client.get(f"/api/campaigns/{campaign_id}/assets").json()
        if a["kind"] == "caption" and a["locale"] == "en-IN"
    ]
    assert captions
    return captions[0]


def test_capabilities_discover_mock_actions_only(client):
    campaign_id, _ = _setup(client)
    caps = client.get(f"/api/campaigns/{campaign_id}/publish/capabilities").json()
    assert caps["is_mock"] is True
    platforms = {a["platform"] for a in caps["actions"]}
    assert "instagram" in platforms
    # Facebook/X do NOT expose write actions even though they are connectors.
    action_ids = {a["action_id"] for a in caps["actions"]}
    assert not any(i.startswith("create_facebook") or i.startswith("create_x_") for i in action_ids)


def test_unverified_asset_cannot_be_prepared(client):
    """Server-side gate: an unresolved (validating) asset is not publishable."""
    campaign_id, poster_id = _setup(client)
    caption = _caption_asset(client, campaign_id)
    assert caption["asset_status"] == "validating"  # never verified

    for asset_id, platform, media_kind in (
        (poster_id, "instagram", "image"),
        (caption["id"], "whatsapp", "text"),
    ):
        response = client.post(
            f"/api/campaigns/{campaign_id}/publish/prepare",
            json={
                "asset_id": asset_id,
                "platform": platform,
                "media_kind": media_kind,
                "owner_uid": "owner-1",
            },
        )
        assert response.status_code == 409, response.text
        body = response.json()["error"]
        assert body["code"] == "asset_not_verified"
        assert body["details"]["asset_status"] == "validating"


def test_failed_asset_cannot_be_prepared_or_approved(client):
    """A caption the Guardian itself FAILED must be unpublishable everywhere."""
    campaign_id, _ = _setup(client)
    from app.db import get_session_factory
    from app.models import AssetRecord

    factsheet = client.get(f"/api/campaigns/{campaign_id}").json()["factsheet"]
    # A caption with a fabricated discount: Guardian → FAIL → asset_status failed.
    with get_session_factory()() as session:
        record = AssetRecord(
            campaign_id=campaign_id,
            factsheet_id=factsheet["id"],
            fact_hash=factsheet["fact_hash"],
            kind="caption",
            locale="en-IN",
            text_content="25% off cold coffee this Saturday & Sunday.",
            asset_status="validating",
            provider="test_fixture",
        )
        session.add(record)
        session.commit()
        failed_id = record.id

    reports = {r["asset_id"]: r["verdict"] for r in _verify_campaign(client, campaign_id)}
    assert reports[failed_id] == "FAIL"
    assets = {a["id"]: a for a in client.get(f"/api/campaigns/{campaign_id}/assets").json()}
    assert assets[failed_id]["asset_status"] == "failed"

    # 1. Cannot be prepared.
    response = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": failed_id, "platform": "whatsapp", "media_kind": "text", "owner_uid": "owner-1"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "asset_not_verified"

    # 2. A record prepared while verified cannot be APPROVED after it fails.
    verified_caption = _caption_asset(client, campaign_id)
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={
            "asset_id": verified_caption["id"],
            "platform": "whatsapp",
            "media_kind": "text",
            "owner_uid": "owner-1",
        },
    )
    assert prepared.status_code == 201, prepared.text
    publish_id = prepared.json()["id"]
    with get_session_factory()() as session:
        record = session.get(AssetRecord, verified_caption["id"])
        record.asset_status = "failed"
        session.commit()
    approved = client.post(f"/api/campaigns/publish/{publish_id}/approve")
    assert approved.status_code == 409, approved.text
    assert approved.json()["error"]["code"] == "asset_not_verified"
    # Execution stays refused as well (the test_integrity_repairs suite covers the
    # approved-then-failed execution path in detail).
    assert client.post(f"/api/campaigns/publish/{publish_id}/execute").status_code == 409


def test_stale_facts_asset_cannot_be_prepared(client):
    """An asset bound to superseded facts is rejected as stale."""
    campaign_id, _ = _setup(client)
    caption = _caption_asset(client, campaign_id)
    _verify_campaign(client, campaign_id)

    from app.db import get_session_factory
    from app.models import AssetRecord

    with get_session_factory()() as session:
        record = session.get(AssetRecord, caption["id"])
        record.fact_hash = "sha256:superseded-fact-hash"  # simulate old fact version
        session.commit()

    response = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": caption["id"], "platform": "whatsapp", "media_kind": "text", "owner_uid": "owner-1"},
    )
    assert response.status_code == 409, response.text
    body = response.json()["error"]
    assert body["code"] == "asset_stale"
    assert body["details"]["locked_fact_hash"]


def test_caption_edit_after_approval_blocks_execution(client):
    """Editing the asset after approval invalidates the approval binding."""
    campaign_id, _ = _setup(client)
    caption = _caption_asset(client, campaign_id)
    _verify_campaign(client, campaign_id)
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": caption["id"], "platform": "whatsapp", "media_kind": "text", "owner_uid": "owner-1"},
    )
    assert prepared.status_code == 201, prepared.text
    publish_id = prepared.json()["id"]
    assert client.post(f"/api/campaigns/publish/{publish_id}/approve").status_code == 200

    from app.db import get_session_factory
    from app.models import AssetRecord

    with get_session_factory()() as session:
        record = session.get(AssetRecord, caption["id"])
        record.text_content = "30% off cold coffee this Saturday & Sunday."  # human edit
        session.commit()

    executed = client.post(f"/api/campaigns/publish/{publish_id}/execute")
    assert executed.status_code == 409, executed.text
    assert executed.json()["error"]["code"] == "approval_stale"


def test_facebook_falls_back_to_manual(client, tesseract_present):
    campaign_id, poster_id = _setup(client)
    _verify_campaign(client, campaign_id)
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": poster_id, "platform": "facebook", "media_kind": "image", "owner_uid": "owner-1"},
    )
    assert prepared.status_code == 201, prepared.text
    data = prepared.json()
    assert data["status"] == "MANUAL_REQUIRED"
    assert "Titan will not fake" in data["response"]["instructions"]


def test_execute_requires_approval(client, tesseract_present):
    campaign_id, poster_id = _setup(client)
    _verify_campaign(client, campaign_id)
    record = _prepare_instagram(client, campaign_id, poster_id)
    response = client.post(f"/api/campaigns/publish/{record['id']}/execute")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_required"


def test_sandbox_publish_success_only_after_response(client, tesseract_present):
    campaign_id, poster_id = _setup(client)
    _verify_campaign(client, campaign_id)
    record = _prepare_instagram(client, campaign_id, poster_id)
    approved = client.post(f"/api/campaigns/publish/{record['id']}/approve")
    assert approved.json()["status"] == "APPROVED"
    executed = client.post(f"/api/campaigns/publish/{record['id']}/execute")
    assert executed.status_code == 200, executed.text
    data = executed.json()
    assert data["status"] == "PUBLISHED"
    assert data["provider_result_ids"]["post_id"].startswith("mock-post-")
    # Idempotent re-execute does not double-publish.
    again = client.post(f"/api/campaigns/publish/{record['id']}/execute")
    assert again.json()["status"] == "PUBLISHED"


def _prepare_instagram(client, campaign_id: int, poster_id: int) -> dict:
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": poster_id, "platform": "sandbox", "media_kind": "image", "owner_uid": "owner-1"},
    )
    assert prepared.status_code == 201, prepared.text
    data = prepared.json()
    assert data["status"] == "READY_FOR_REVIEW"
    return data


def test_whatsapp_wa_me_link(client):
    campaign_id, _ = _setup(client)
    caption = _caption_asset(client, campaign_id)
    _verify_campaign(client, campaign_id)
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": caption["id"], "platform": "whatsapp", "media_kind": "text", "owner_uid": "owner-1"},
    )
    assert prepared.status_code == 201, prepared.text
    record = prepared.json()
    record = client.post(f"/api/campaigns/publish/{record['id']}/approve").json()
    executed = client.post(f"/api/campaigns/publish/{record['id']}/execute").json()
    assert executed["status"] == "PUBLISHED"
    assert "wa.me/?text=" in executed["response"]["wa_me_url"]


def test_mock_mode_blocks_social_destinations(client, tesseract_present):
    """In mock mode a REAL social destination never reports PUBLISHED."""
    campaign_id, poster_id = _setup(client)
    _verify_campaign(client, campaign_id)
    prepared = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": poster_id, "platform": "instagram", "media_kind": "image", "owner_uid": "owner-1"},
    )
    assert prepared.status_code == 201, prepared.text
    publish_id = prepared.json()["id"]
    assert client.post(f"/api/campaigns/publish/{publish_id}/approve").status_code == 200
    executed = client.post(f"/api/campaigns/publish/{publish_id}/execute")
    assert executed.status_code == 409, executed.text
    assert executed.json()["error"]["code"] == "mock_publish_blocked"
    # And it is NOT published.
    listed = client.get(f"/api/campaigns/{campaign_id}/publish/export").json()
    statuses = [r["status"] for r in listed["publish_records"]]
    assert "PUBLISHED" not in statuses


def test_export_package(client):
    campaign_id, _ = _setup(client)
    export = client.get(f"/api/campaigns/{campaign_id}/publish/export").json()
    assert export["captions"]
    assert "Titan never fakes" in export["generated_notice"]
    assert export["assets"]
