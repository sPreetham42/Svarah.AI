"""FactSheet API workflow: extract → edit → lock → re-edit → re-lock."""

from __future__ import annotations

import base64

DEMO_TRANSCRIPT = (
    "Mock transcript: 20% off cold coffee this Saturday and Sunday, "
    "4 PM to 8 PM at our cafe for college students."
)
FAKE_AUDIO_B64 = base64.b64encode(b"\x1aE\xdf\xa3fake-audio").decode()


def make_campaign(client, text: str = DEMO_TRANSCRIPT) -> dict:
    response = client.post("/api/campaigns", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


def sid(campaign: dict) -> int:
    return campaign["factsheet"]["id"]


# ── draft retrieval ───────────────────────────────────────────────────
def test_campaign_returns_draft_factsheet_and_versions(client):
    campaign = make_campaign(client)
    sheet = campaign["factsheet"]
    assert sheet["status"] == "draft"
    assert sheet["version"] == 1
    assert sheet["fact_hash"] is None
    assert sheet["seal"] is None
    assert sheet["tokens"] is None
    assert sheet["extraction"]["status"] == "ok"
    assert sheet["extraction"]["is_mock"] is True
    assert len(campaign["factsheet_versions"]) == 1
    assert campaign["factsheet_versions"][0]["status"] == "draft"


def test_get_factsheet_by_id(client):
    campaign = make_campaign(client)
    response = client.get(f"/api/factsheets/{sid(campaign)}")
    assert response.status_code == 200
    assert response.json()["campaign_id"] == campaign["id"]


def test_get_unknown_factsheet_404(client):
    response = client.get("/api/factsheets/987654")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


# ── human edits ───────────────────────────────────────────────────────
def test_patch_draft_normalizes_and_clears_metadata(client):
    campaign = make_campaign(client)
    sheet_id = sid(campaign)

    response = client.patch(
        f"/api/factsheets/{sheet_id}",
        json={
            "offer": {"discount_percent": 15, "product": ["Iced tea", "Cake"]},
            "languages": ["kn", "Hindi"],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["version"] == 1
    assert body["facts"]["offer"]["discount_percent"] == 15
    assert body["facts"]["offer"]["product"] == ["Iced tea", "Cake"]
    assert body["facts"]["languages"] == ["Kannada", "Hindi"]
    # human edits are no longer model-inferred/confident
    assert "offer.discount_percent" not in body["facts"]["extraction_confidence"]


def test_patch_empty_is_rejected(client):
    campaign = make_campaign(client)
    response = client.patch(f"/api/factsheets/{sid(campaign)}", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_patch"


def test_patch_unknown_field_is_rejected(client):
    campaign = make_campaign(client)
    response = client.patch(
        f"/api/factsheets/{sid(campaign)}", json={"offer": {"bogus_field": 1}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_patch_invalid_value_is_rejected(client):
    campaign = make_campaign(client)
    response = client.patch(
        f"/api/factsheets/{sid(campaign)}", json={"offer": {"discount_percent": 200}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "normalization_error"


def test_patch_unknown_factsheet_404(client):
    response = client.patch("/api/factsheets/987654", json={"languages": ["Hindi"]})
    assert response.status_code == 404


# ── locking ───────────────────────────────────────────────────────────
def test_lock_complete_facts(client):
    campaign = make_campaign(client)
    sheet_id = sid(campaign)

    response = client.post(f"/api/factsheets/{sheet_id}/lock")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["status"] == "locked"
    assert body["fact_hash"].startswith("sha256:")
    assert len(body["fact_hash"]) == len("sha256:") + 64
    assert body["seal"].startswith("hmac-sha256:")
    assert body["seal_algorithm"] == "hmac-sha256"
    assert body["seal_valid"] is True
    assert body["locked_at"] is not None

    tokens = body["tokens"]
    assert tokens["DISCOUNT"] == "20%"
    assert tokens["PRODUCT"] == "cold coffee"
    assert tokens["DAYS"] == "Saturday & Sunday"
    assert tokens["WINDOW"] == "4 PM–8 PM"

    # campaign state follows
    refreshed = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert refreshed["status"] == "locked"

    # the secret is never exposed
    assert "test-seal-secret" not in response.text


def test_lock_is_idempotent(client):
    campaign = make_campaign(client)
    sheet_id = sid(campaign)
    first = client.post(f"/api/factsheets/{sheet_id}/lock").json()
    second = client.post(f"/api/factsheets/{sheet_id}/lock").json()
    assert first["fact_hash"] == second["fact_hash"]
    assert first["seal"] == second["seal"]
    versions = client.get(f"/api/campaigns/{campaign['id']}").json()["factsheet_versions"]
    assert len(versions) == 1


def test_lock_incomplete_facts_rejected(client):
    campaign = make_campaign(client, "We are open as usual.")
    response = client.post(f"/api/factsheets/{sid(campaign)}/lock")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "incomplete_factsheet"
    assert "offer.product" in body["error"]["details"]["missing"]


def test_lock_without_secret_fails_safely(client, no_seal_secret):
    campaign = make_campaign(client)  # extraction does not need the seal
    response = client.post(f"/api/factsheets/{sid(campaign)}/lock")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "seal_unavailable"
    assert body["error"]["details"]["configured"] is False

    # nothing was half-locked
    refreshed = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert refreshed["status"] == "extracted"
    assert refreshed["factsheet"]["status"] == "draft"
    assert refreshed["factsheet"]["fact_hash"] is None


# ── locked-version immutability & re-lock workflow ───────────────────
def test_patch_locked_creates_new_draft_and_preserves_locked(client):
    campaign = make_campaign(client)
    locked = client.post(f"/api/factsheets/{sid(campaign)}/lock").json()
    original_hash = locked["fact_hash"]

    response = client.patch(
        f"/api/factsheets/{locked['id']}", json={"offer": {"discount_percent": 25}}
    )
    assert response.status_code == 200
    new_draft = response.json()
    assert new_draft["status"] == "draft"
    assert new_draft["version"] == locked["version"] + 1
    assert new_draft["facts"]["offer"]["discount_percent"] == 25
    assert new_draft["fact_hash"] is None

    # the original locked version is untouched, only marked superseded
    original = client.get(f"/api/factsheets/{locked['id']}").json()
    assert original["status"] == "superseded"
    assert original["fact_hash"] == original_hash
    assert original["seal_valid"] is True

    # campaign is back to an editable draft
    refreshed = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert refreshed["status"] == "extracted"
    assert refreshed["factsheet"]["id"] == new_draft["id"]

    # re-lock the new version with a different hash
    relocked = client.post(f"/api/factsheets/{new_draft['id']}/lock").json()
    assert relocked["status"] == "locked"
    assert relocked["fact_hash"] != original_hash
    assert relocked["tokens"]["DISCOUNT"] == "25%"


def test_superseded_version_cannot_be_locked_or_edited(client):
    campaign = make_campaign(client)
    old_id = sid(campaign)
    client.post(f"/api/factsheets/{old_id}/lock")
    client.patch(f"/api/factsheets/{old_id}", json={"offer": {"discount_percent": 10}})

    lock_response = client.post(f"/api/factsheets/{old_id}/lock")
    assert lock_response.status_code == 409
    assert lock_response.json()["error"]["code"] == "superseded_version"

    patch_response = client.patch(
        f"/api/factsheets/{old_id}", json={"offer": {"discount_percent": 5}}
    )
    assert patch_response.status_code == 409


def test_hash_is_reproducible_across_campaigns(client):
    first = client.post(f"/api/factsheets/{sid(make_campaign(client))}/lock").json()
    second = client.post(f"/api/factsheets/{sid(make_campaign(client))}/lock").json()
    assert first["fact_hash"] == second["fact_hash"]
    assert first["seal"] == second["seal"]


# ── extraction endpoint ───────────────────────────────────────────────
def test_extract_endpoint_reruns_extraction(client):
    campaign = make_campaign(client)
    response = client.post(f"/api/campaigns/{campaign['id']}/extract")
    assert response.status_code == 200
    assert response.json()["factsheet"]["status"] == "draft"
    assert response.json()["factsheet"]["extraction"]["status"] == "ok"


def test_extract_without_transcript_is_rejected(client, env_override):
    env_override(TITAN_STT_PROVIDER="none")
    created = client.post(
        "/api/campaigns", json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"}
    ).json()
    assert created["transcript"] is None

    response = client.post(f"/api/campaigns/{created['id']}/extract")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "missing_transcript"


def test_extract_when_locked_is_conflict(client):
    campaign = make_campaign(client)
    client.post(f"/api/factsheets/{sid(campaign)}/lock")
    response = client.post(f"/api/campaigns/{campaign['id']}/extract")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "facts_locked"


# ── manual entry fallback ─────────────────────────────────────────────
def test_manual_entry_when_extraction_unavailable(client, env_override):
    env_override(TITAN_EXTRACTION_PROVIDER="none")
    campaign = make_campaign(client)

    sheet = campaign["factsheet"]
    assert sheet is not None
    assert sheet["status"] == "draft"
    assert sheet["extraction"]["status"] == "unavailable"
    assert sheet["extraction"]["fallback"] == "manual_entry"
    assert sheet["facts"]["offer"]["product"] == []
    assert campaign["status"] == "extracted"

    # The owner enters facts manually, then locks.
    response = client.patch(
        f"/api/factsheets/{sheet['id']}",
        json={
            "offer": {
                "product": ["Thali"],
                "discount_percent": 10,
                "days": ["Monday"],
            }
        },
    )
    assert response.status_code == 200
    locked = client.post(f"/api/factsheets/{sheet['id']}/lock")
    assert locked.status_code == 200
    assert locked.json()["tokens"]["PRODUCT"] == "Thali"


# ── modes reporting ───────────────────────────────────────────────────
def test_modes_reports_seal_and_extraction_without_secret(client):
    response = client.get("/api/modes")
    assert response.status_code == 200
    body = response.json()
    assert body["seal"]["configured"] is True
    assert body["extraction"]["name"] == "extraction"
    assert "test-seal-secret" not in response.text
