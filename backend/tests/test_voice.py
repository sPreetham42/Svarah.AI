"""Phase 5A/5B voice tests: consent, ownership, reuse, generation, provenance."""

from __future__ import annotations

import base64
import io
import wave


def _wav_b64() -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 8000)  # 1 s of quiet noise
    return base64.b64encode(buffer.getvalue()).decode()


def _setup_campaign(client) -> int:
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.patch(
        f"/api/factsheets/{sheet_id}", json={"languages": ["English", "Hindi", "Kannada", "Tamil", "Telugu"]}
    )
    client.post(f"/api/factsheets/{sheet_id}/lock")
    client.post(f"/api/campaigns/{campaign_id}/plan")
    return campaign_id


def test_profile_requires_consent(client):
    response = client.post(
        "/api/voice-profiles",
        json={
            "owner_uid": "owner-1",
            "display_name": "Shopkeeper",
            "consent_confirmed": False,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"


def test_profile_create_mock_reuses_voice_id(client, other_account):
    audio = _wav_b64()
    response = client.post(
        "/api/voice-profiles",
        json={
            "display_name": "Shopkeeper",
            "consent_confirmed": True,
            "consent_record": {"source": "in_app_recording", "language": "en-IN"},
            "reference_audio_b64": audio,
            "reference_audio_mime": "audio/wav",
            "reference_transcript": "Hello, this is my shop voice.",
        },
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["is_mock"] is True
    assert profile["provider_voice_id"] and profile["provider_voice_id"].startswith("mock-voice-")
    assert profile["consent_confirmed"] is True

    # Listed only for its owner — scoped by the authenticated account, not by
    # any identifier the caller supplies.
    mine = client.get("/api/voice-profiles").json()
    assert len(mine) == 1
    other = client.get("/api/voice-profiles", headers=other_account).json()
    assert other == []

    # And the second account cannot read it by id either.
    assert client.get(
        f"/api/voice-profiles/{profile['id']}", headers=other_account
    ).status_code == 404


def test_localized_voice_generation_provenance(client):
    campaign_id = _setup_campaign(client)
    profile = client.post(
        "/api/voice-profiles",
        json={
            "owner_uid": "owner-1",
            "display_name": "Shopkeeper",
            "consent_confirmed": True,
            "consent_record": {"source": "in_app_recording", "language": "en-IN"},
            "reference_audio_b64": _wav_b64(),
            "reference_audio_mime": "audio/wav",
        },
    ).json()

    response = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "English"},
    )
    assert response.status_code == 201, response.text
    asset = response.json()
    assert asset["kind"] == "voice"
    assert asset["locale"] == "en-IN"
    assert asset["is_mock"] is True
    provenance = asset["provenance"]
    assert provenance["voice_profile_id"] == profile["id"]
    assert provenance["provider_voice_id"] == profile["provider_voice_id"]
    assert provenance["language_code"] == "en-IN"
    assert provenance["labeled"] == "mock_cloned_voice"
    assert asset["sha256"].startswith("sha256:")
    # Substituted script must contain locked values, not raw tokens.
    assert "20%" in asset["text_content"]
    assert "{{" not in asset["text_content"]


def test_voice_ownership_enforced(client, other_account):
    """A second account can neither use the profile nor reach the campaign."""
    campaign_id = _setup_campaign(client)
    profile = client.post(
        "/api/voice-profiles",
        json={"display_name": "A", "consent_confirmed": True},
    ).json()

    # The campaign itself is invisible to the other account.
    response = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "language": "English"},
        headers=other_account,
    )
    assert response.status_code == 404, response.text

    # Cross-owner generation must produce no asset.
    listed = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert all(a["kind"] != "voice" for a in listed)


def test_voice_profile_of_another_account_is_unusable(client, other_account):
    """Owning the campaign is not enough: the profile must be yours too."""
    campaign_id = _setup_campaign(client)
    foreign_profile = client.post(
        "/api/voice-profiles",
        json={"display_name": "Not yours", "consent_confirmed": True},
        headers=other_account,
    ).json()

    response = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": foreign_profile["id"], "language": "English"},
    )
    assert response.status_code == 404, response.text
    listed = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert all(a["kind"] != "voice" for a in listed)


def test_unsupported_language_refused(client):
    campaign_id = _setup_campaign(client)
    profile = client.post(
        "/api/voice-profiles",
        json={"owner_uid": "owner-1", "display_name": "A", "consent_confirmed": True},
    ).json()
    response = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "French"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "language_unsupported"


def test_voice_id_missing_conflict(client):
    campaign_id = _setup_campaign(client)
    profile = client.post(
        "/api/voice-profiles",
        json={"owner_uid": "owner-1", "display_name": "A", "consent_confirmed": True},
    ).json()
    response = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "Hindi"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "voice_id_missing"


def test_profile_deletion_removes_reference_audio(client):
    profile = client.post(
        "/api/voice-profiles",
        json={
            "owner_uid": "owner-1",
            "display_name": "A",
            "consent_confirmed": True,
            "reference_audio_b64": _wav_b64(),
            "reference_audio_mime": "audio/wav",
        },
    ).json()
    response = client.delete(
        f"/api/voice-profiles/{profile['id']}", params={"owner_uid": "owner-1"}
    )
    assert response.status_code == 204
    data = client.get("/api/voice-profiles", params={"owner_uid": "owner-1"}).json()
    assert data == []  # deleted profiles are not listed


def test_clone_chunking_limits():
    from app.services.sarvam import split_for_clone, MAX_CLONE_TEXT_CHARS

    text = ("This is a long sentence with words. " * 60).strip()
    chunks = split_for_clone(text)
    assert all(len(c) <= MAX_CLONE_TEXT_CHARS for c in chunks)
    assert len(chunks) > 1
    joined = " ".join(chunks)
    assert sorted(joined.split()) == sorted(text.split())  # no content lost
