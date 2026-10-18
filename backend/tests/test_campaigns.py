"""Campaign creation/retrieval and validation tests."""

from __future__ import annotations

import base64
import hashlib

from app.errors import STTUnavailableError

SAMPLE_TEXT = "20% off cold coffee on Saturday and Sunday, 4 PM to 8 PM."

FAKE_AUDIO = b"\x1aE\xdf\xa3fake-webm-audio-bytes"
FAKE_AUDIO_B64 = base64.b64encode(FAKE_AUDIO).decode()


def _create_typed(client, text: str = SAMPLE_TEXT, **extra):
    payload = {"text": text, **extra}
    return client.post("/api/campaigns", json=payload)


def test_create_typed_campaign(client):
    response = _create_typed(client)
    assert response.status_code == 201
    body = response.json()

    assert body["status"] == "extracted"  # mock extraction runs on create
    assert body["input_type"] == "typed"
    assert body["transcript"]["raw"] == SAMPLE_TEXT
    assert body["transcript"]["normalized"] == SAMPLE_TEXT
    assert body["transcript"]["provider"] == "typed"
    assert body["transcript"]["is_mock"] is False

    expected_hash = "sha256:" + hashlib.sha256(SAMPLE_TEXT.encode()).hexdigest()
    assert body["transcript"]["hash"] == expected_hash

    # Extracted draft facts are schema-validated; creative assets stay empty.
    assert body["facts"]["offer"]["discount_percent"] == 20
    assert body["factsheet"]["status"] == "draft"
    assert body["factsheet"]["version"] == 1
    assert body["assets"] == []
    assert body["verification"] == []

    event_types = [e["event_type"] for e in body["audit_events"]]
    assert "campaign.created" in event_types
    assert "transcript.captured" in event_types
    assert "facts.extracted" in event_types


def test_get_campaign_by_id(client):
    created = _create_typed(client, "Flat 100 rupees off on school bags.").json()
    response = client.get(f"/api/campaigns/{created['id']}")
    assert response.status_code == 200
    assert response.json()["id"] == created["id"]
    assert response.json()["transcript"]["raw"] == "Flat 100 rupees off on school bags."


def test_list_campaigns(client):
    first = _create_typed(client, "Offer one").json()
    second = _create_typed(client, "Offer two").json()
    response = client.get("/api/campaigns")
    assert response.status_code == 200
    ids = [c["id"] for c in response.json()]
    assert first["id"] in ids and second["id"] in ids


def test_default_shop_is_created_once(client):
    first = _create_typed(client, "Offer one").json()
    second = _create_typed(client, "Offer two").json()
    assert first["shop_id"] == second["shop_id"]


def test_create_with_explicit_unknown_shop_fails(client):
    response = client.post(
        "/api/campaigns", json={"text": "Offer", "shop_id": 999999}
    )
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["details"]["shop_id"] == 999999


def test_empty_request_is_rejected(client):
    response = client.post("/api/campaigns", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_whitespace_only_text_is_rejected(client):
    response = client.post("/api/campaigns", json={"text": "   \n  "})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_base64_audio_is_rejected(client):
    response = client.post(
        "/api/campaigns", json={"audio_b64": "not-valid-base64!!"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_audio"


def test_missing_campaign_returns_not_found(client):
    response = client.get("/api/campaigns/424242")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["details"]["campaign_id"] == 424242


def test_invalid_campaign_id_type_is_rejected(client):
    response = client.get("/api/campaigns/not-a-number")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# ── STT-failure recovery (Catch-22 regression) ────────

def _create_audio_campaign_with_stt_failing(client, monkeypatch):
    """Create an audio campaign whose transcription fails (STT unavailable)."""

    class _RefusingSTT:
        name = "refusing"
        code = "stt_unavailable"
        provider = "refusing"
        message = "Speech-to-text is unavailable in tests."

        async def transcribe(self, *args, **kwargs):
            raise STTUnavailableError(
                self.message, details={"reason": "test_refusal"}
            )

    from app.api import campaigns as campaigns_module
    monkeypatch.setattr(
        campaigns_module, "get_stt_provider", lambda settings=None: _RefusingSTT()
    )

    response = client.post(
        "/api/campaigns",
        json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"},
    )
    assert response.status_code == 201
    return response.json()


def test_audio_campaign_with_failed_stt_still_has_a_draft_factsheet(
    client, monkeypatch
):
    """STT failure must never strand the client without a FactSheet.

    Regression for the Catch-22: the old code skipped extraction when the
    transcript was missing, so a 201 response carried no factsheet and the
    frontend review screen dead-ended on "re-run extraction".
    """
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    assert body["transcript"] is None
    assert body["stt"]["status"] == "unavailable"
    # The manual-entry escape hatch exists: a truthful, empty draft sheet.
    assert body["factsheet"] is not None
    assert body["factsheet"]["status"] == "draft"
    assert body["factsheet"]["extraction"]["status"] == "unavailable"
    assert body["factsheet"]["extraction"]["fallback"] == "manual_entry"
    assert body["facts"]["offer"]["product"] == []

    event_types = [e["event_type"] for e in body["audit_events"]]
    assert "stt.unavailable" in event_types
    assert "facts.extracted" in event_types


def test_audio_campaign_recovery_endpoint_attaches_typed_transcript(
    client, monkeypatch
):
    """The typed-transcript endpoint must break the Catch-22 loop."""
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]

    response = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert response.status_code == 200
    recovered = response.json()

    assert recovered["input_type"] == "typed"
    assert recovered["transcript"]["raw"] == SAMPLE_TEXT
    assert recovered["stt"]["status"] == "skipped"
    # Extraction ran on the recovered transcript.
    assert recovered["factsheet"]["extraction"]["status"] == "ok"
    assert recovered["factsheet"]["facts"]["offer"]["discount_percent"] == 20


def test_transcript_recovery_endpoint_rejects_empty_text(client, monkeypatch):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)

    response = client.post(
        f"/api/campaigns/{body['id']}/transcript", json={"text": "   "}
    )
    assert response.status_code == 422


def test_transcript_recovery_endpoint_rejects_an_existing_transcript(
    client, monkeypatch
):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]

    first = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": "A second brief should not overwrite the first."},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "transcript_exists"


def test_transcript_recovery_endpoint_is_not_available_to_other_accounts(
    client, monkeypatch, other_account
):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)

    response = client.post(
        f"/api/campaigns/{body['id']}/transcript",
        json={"text": SAMPLE_TEXT},
        headers=other_account,
    )
    assert response.status_code == 404


def test_recovery_endpoint_refuses_when_facts_are_locked(client, monkeypatch):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]
    sheet_id = body["factsheet"]["id"]

    patch = client.patch(
        f"/api/factsheets/{sheet_id}",
        json={"offer": {"discount_percent": 20, "product": ["cold coffee"]}},
    )
    assert patch.status_code == 200
    locked = client.post(f"/api/factsheets/{sheet_id}/lock", json={})
    assert locked.status_code == 200

    response = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "facts_locked"


# ── STT-failure recovery (Catch-22 regression) ────────


def _create_audio_campaign_with_stt_failing(client, monkeypatch):
    """Create an audio campaign whose transcription fails (STT unavailable)."""

    class _RefusingSTT:
        name = "refusing"
        code = "stt_unavailable"
        provider = "refusing"
        message = "Speech-to-text is unavailable in tests."

        async def transcribe(self, *args, **kwargs):
            raise STTUnavailableError(
                self.message, details={"reason": "test_refusal"}
            )

    from app.api import campaigns as campaigns_module
    monkeypatch.setattr(
        campaigns_module, "get_stt_provider", lambda settings=None: _RefusingSTT()
    )

    response = client.post(
        "/api/campaigns",
        json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"},
    )
    assert response.status_code == 201
    return response.json()


def test_audio_campaign_with_failed_stt_still_has_a_draft_factsheet(
    client, monkeypatch
):
    """STT failure must never strand the client without a FactSheet.

    Regression for the Catch-22: the old code skipped extraction when the
    transcript was missing, so a 201 response carried no factsheet and the
    frontend review screen dead-ended on "re-run extraction".
    """
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)

    assert body["transcript"] is None
    assert body["stt"]["status"] == "unavailable"
    # The manual-entry escape hatch exists: a truthful, empty draft sheet.
    assert body["factsheet"] is not None
    assert body["factsheet"]["status"] == "draft"
    assert body["factsheet"]["extraction"]["status"] == "unavailable"
    assert body["factsheet"]["extraction"]["fallback"] == "manual_entry"
    assert body["facts"]["offer"]["product"] == []

    event_types = [e["event_type"] for e in body["audit_events"]]
    assert "stt.unavailable" in event_types
    assert "facts.extracted" in event_types


def test_audio_campaign_recovery_endpoint_attaches_typed_transcript(
    client, monkeypatch
):
    """The typed-transcript endpoint must break the Catch-22 loop."""
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]

    response = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert response.status_code == 200
    recovered = response.json()

    assert recovered["input_type"] == "typed"
    assert recovered["transcript"]["raw"] == SAMPLE_TEXT
    assert recovered["stt"]["status"] == "skipped"
    # Extraction ran on the recovered transcript.
    assert recovered["factsheet"]["extraction"]["status"] == "ok"
    assert recovered["factsheet"]["facts"]["offer"]["discount_percent"] == 20


def test_transcript_recovery_endpoint_rejects_empty_text(client, monkeypatch):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)

    response = client.post(
        f"/api/campaigns/{body['id']}/transcript", json={"text": "   "}
    )
    assert response.status_code == 422


def test_transcript_recovery_endpoint_rejects_an_existing_transcript(
    client, monkeypatch
):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]

    first = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": "A second brief should not overwrite the first."},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "transcript_exists"


def test_transcript_recovery_endpoint_is_not_available_to_other_accounts(
    client, monkeypatch, other_account
):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)

    response = client.post(
        f"/api/campaigns/{body['id']}/transcript",
        json={"text": SAMPLE_TEXT},
        headers=other_account,
    )
    assert response.status_code == 404


def test_recovery_endpoint_refuses_when_facts_are_locked(client, monkeypatch):
    body = _create_audio_campaign_with_stt_failing(client, monkeypatch)
    campaign_id = body["id"]
    sheet_id = body["factsheet"]["id"]
    # Fill the minimum lockable facts, then lock.
    patch = client.patch(
        f"/api/factsheets/{sheet_id}",
        json={"offer": {"discount_percent": 20, "product": ["cold coffee"]}},
    )
    assert patch.status_code == 200
    locked = client.post(f"/api/factsheets/{sheet_id}/lock", json={})
    assert locked.status_code == 200

    response = client.post(
        f"/api/campaigns/{campaign_id}/transcript",
        json={"text": SAMPLE_TEXT},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "facts_locked"


from app.errors import STTUnavailableError  # noqa: E402  (imports above need clients)

FAKE_AUDIO = b"\x1aE\xdf\xa3fake-webm-audio-bytes"
FAKE_AUDIO_B64 = base64.b64encode(FAKE_AUDIO).decode()
