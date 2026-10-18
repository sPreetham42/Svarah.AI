"""Phase 3 campaign-plan tests: token firewall, provenance, localization."""

from __future__ import annotations

import json

import pytest


def _create_campaign(client, text: str | None = None) -> int:
    payload = {"text": text} if text else {
        "audio_b64": "c2hvcnBrZWVwZXIgcGVhayW=",  # not used; typed by default when text provided
    }
    if not text:
        payload = {"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."}
    response = client.post("/api/campaigns", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _lock(client, campaign_id: int) -> dict:
    """Set campaign languages to the targeted set, then lock the active draft."""
    from app.db import get_session_factory
    from app.models import FactSheetRecord, FactSheetStatus

    with get_session_factory()() as session:
        sheet = (
            session.query(FactSheetRecord)
            .filter(FactSheetRecord.status == FactSheetStatus.DRAFT)
            .order_by(FactSheetRecord.id.desc())
            .first()
        )
        assert sheet is not None
        sheet_id = sheet.id
    response = client.patch(
        f"/api/factsheets/{sheet_id}",
        json={"languages": ["English", "Hindi", "Kannada", "Tamil", "Telugu"]},
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/api/factsheets/{sheet_id}/lock")
    assert response.status_code == 200, response.text
    return response.json()


def test_plan_requires_locked_facts(client):
    campaign_id = _create_campaign(client)
    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "facts_not_locked"


def test_mock_plan_generates_token_safe_copy(client):
    campaign_id = _create_campaign(client)
    sheet = _lock(client, campaign_id)
    assert sheet["status"] == "locked"

    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan["is_mock"] is True
    assert plan["fact_hash"] == sheet["fact_hash"]
    assert plan["fact_sheet_id"] == sheet["id"]

    # Templates embed no literal fact values — facts live only in {{TOKENS}}.
    # The reel script carries internal scene labels ("Scene 1:") which are
    # production metadata and are stripped by the Guardian before claim
    # analysis, so digits are allowed ONLY inside those labels.
    import re

    scene_label = re.compile(r"Scene\s+\d+\s*:")
    for channel, entry in plan["copy_templates"].items():
        template = entry["template"]
        assert not any(ch.isdigit() for ch in scene_label.sub("", template)), (channel, template)
    substituted = client.get(f"/api/campaigns/{campaign_id}/plan").json()["substituted"]
    assert "20%" in substituted["master"]["poster_headline"]
    assert "cold coffee" in substituted["master"]["instagram"]


def test_plan_is_persisted_and_reusable(client):
    campaign_id = _create_campaign(client)
    _lock(client, campaign_id)
    first = client.post(f"/api/campaigns/{campaign_id}/plan").json()
    second = client.post(f"/api/campaigns/{campaign_id}/plan").json()
    assert first["id"] == second["id"]
    got = client.get(f"/api/campaigns/{campaign_id}/plan").json()
    assert got["plan"]["version"] == first["version"]


def test_plan_provenance_survives_a_reload(client):
    """A re-read plan must keep its own mock/live label.

    The plan row records who produced it; GET /plan must report that rather than
    defaulting to a placeholder, so a mock plan can never be shown as live.
    """
    campaign_id = _create_campaign(client)
    _lock(client, campaign_id)
    created = client.post(f"/api/campaigns/{campaign_id}/plan").json()

    got = client.get(f"/api/campaigns/{campaign_id}/plan").json()["plan"]
    assert got["is_mock"] == created["is_mock"]
    assert got["provider"] == created["provider"]
    assert got["provider"] != "unknown"
    assert got["model"] == created["model"]


def test_localization_specs_present_for_targeted_languages(client):
    campaign_id = _create_campaign(client)
    _lock(client, campaign_id)
    client.patch  # languages were normalized at create; lock as-is
    plan = client.post(f"/api/campaigns/{campaign_id}/plan").json()
    languages = [loc["language"] for loc in plan["localization"]]
    assert languages, "expected at least one localization spec"
    for loc in plan["localization"]:
        assert loc["locale"]
        assert isinstance(loc["tone"], list)
        localization_templates = loc["copy_templates"]
        assert "voice_script" in localization_templates


def test_plan_seal_tamper_refusal(client, monkeypatch):
    campaign_id = _create_campaign(client)
    sheet = _lock(client, campaign_id)

    from app.db import get_session_factory
    from app.models import FactSheetRecord

    with get_session_factory()() as session:
        record = session.get(FactSheetRecord, sheet["id"])
        record.facts_json = json.dumps(json.loads(record.facts_json) | {"offer": {"product": ["Tampered Item"], "discount_percent": 25}})
        session.commit()

    response = client.post(f"/api/campaigns/{campaign_id}/plan")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "seal_invalid"


def test_unresolved_token_in_copied_template_is_refused(client):
    """substitute_copy fails closed when a template references an absent token."""
    from app.services.campaign_brain import TokenizedCopy, substitute_copy
    from app.errors import TokenError

    bad = {"instagram": TokenizedCopy(channel="instagram", template="{{PRICE}} off!")}
    with pytest.raises(TokenError):
        substitute_copy(bad, {"PRODUCT": "Tea", "DISCOUNT": "10%"})
