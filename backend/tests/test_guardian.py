"""Phase 7 Guardian tests: mutations, certificate, repair, sabotage."""

from __future__ import annotations

import base64
import io
import wave


TOKENS = {
    "PRODUCT": "cold coffee",
    "DISCOUNT": "20%",
    "DAYS": "Saturday & Sunday",
    "WINDOW": "4 PM–8 PM",
    "AUDIENCE": "college students",
}


def _setup_full(client) -> tuple[int, dict]:
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.patch(f"/api/factsheets/{sheet_id}", json={"languages": ["English", "Hindi"]})
    client.post(f"/api/factsheets/{sheet_id}/lock")
    client.post(f"/api/campaigns/{campaign_id}/plan")
    client.post(f"/api/campaigns/{campaign_id}/assets/posters", json={"variants": 1})
    client.post(f"/api/campaigns/{campaign_id}/assets/captions")
    profile = client.post(
        "/api/voice-profiles",
        json={
            "owner_uid": "owner-1",
            "display_name": "Shopkeeper",
            "consent_confirmed": True,
            "reference_audio_b64": base64.b64encode(
                _wav()
            ).decode(),
            "reference_audio_mime": "audio/wav",
        },
    ).json()
    _ = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "English"},
    ).json()
    return campaign_id, {"profile_id": profile["id"]}


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 16000)
    return buffer.getvalue()


# ── deterministic text checks ─────────────────────────────────────────
def test_numeric_mutation_fails(client):
    from app.guardian.checks_text import verify_text

    clean_text = "20% off cold coffee this Saturday & Sunday, 4 PM–8 PM."
    mutated_text = "25% off cold coffee this Saturday & Sunday."
    assert all(o.verdict == "PASS" for o in verify_text(clean_text, TOKENS))
    report = {o.check: o.verdict for o in verify_text(mutated_text, TOKENS)}
    assert report["numeric_parity"] == "FAIL"


def test_unsourced_number_and_claim_fail(client):
    from app.guardian.checks_text import verify_text

    bad_text = "Best in town! Only for first 50 students, 999 rupees welcome drink."
    report = {o.check: o.verdict for o in verify_text(bad_text, TOKENS)}
    assert report["numeric_parity"] == "FAIL"
    assert report["unsupported_claims"] == "FAIL"


def test_locked_condition_claim_allowed(client):
    from app.guardian.checks_text import verify_text

    text_with_condition = "20% off cold coffee. Minimum order 200."
    report = {
        o.check: o.verdict
        for o in verify_text(text_with_condition, TOKENS, conditions=["Minimum order 200"])
    }
    # "200" maps to the locked condition text — parity catches it via numeric map
    # only when it's a token; conditions with numbers are advisory here.
    assert report["unsupported_claims"] == "PASS"


def test_wrong_weekday_fails(client):
    from app.guardian.checks_text import verify_text

    text = "20% off cold coffee this Wednesday!"
    report = {o.check: o.verdict for o in verify_text(text, TOKENS)}
    assert report["fact_parity_days"] == "FAIL"


# ── campaign-level verification + certificate ─────────────────────────
def test_verify_and_certificate(client, monkeypatch):
    campaign_id, _ = _setup_full(client)
    response = client.post(f"/api/campaigns/{campaign_id}/verify")
    assert response.status_code == 200, response.text
    reports = response.json()
    assert reports, "expected at least one report"

    certificate = client.post(f"/api/campaigns/{campaign_id}/certificate")
    assert certificate.status_code == 200, certificate.text
    payload = certificate.json()
    assert payload["factsheet"]["fact_hash"]
    assert payload["integrity_seal"].startswith("hmac-sha256:")
    html = client.get(f"/api/campaigns/{campaign_id}/certificate/html")
    assert html.status_code == 200
    assert "Titan Verification Certificate" in html.text


def test_certificate_tamper_detection(client):
    campaign_id, _ = _setup_full(client)
    from app.guardian.certificate import build_certificate, certificate_valid
    from app.db import get_session_factory

    client.post(f"/api/campaigns/{campaign_id}/certificate")
    with get_session_factory()() as session:
        from app.models import AssetRecord

        record = (
            session.query(AssetRecord)
            .filter(AssetRecord.kind == "poster")
            .order_by(AssetRecord.id.desc())
            .first()
        )
        # Simulate an unauthorized file change in storage.
        from app.storage import get_storage

        get_storage().save_bytes(b"\x89PNG corrupted bytes", record.storage_path)
    certificate = build_certificate(session, campaign_id)
    validity = certificate_valid(certificate)
    assert validity["assets_unchanged"] is False
    assert validity["tampered"]


def test_sabotage_demo_end_to_end(client, monkeypatch):
    campaign_id, _ = _setup_full(client)
    captions = [
        a for a in client.get(f"/api/campaigns/{campaign_id}/assets").json() if a["kind"] == "caption"
    ]
    assert captions
    target = next(c for c in captions if "20%" in (c["text_content"] or ""))

    # Disabled by default → refuses.
    refused = client.post(
        f"/api/demo/sabotage/{target['id']}", json={"mode": "discount", "owner_uid": "owner-1"}
    )
    assert refused.status_code == 409

    monkeypatch.setenv("TITAN_ENABLE_DEMO_SABOTAGE", "true")
    from app import config

    config.reset_settings_cache()
    try:
        run = client.post(
            f"/api/demo/sabotage/{target['id']}", json={"mode": "discount", "owner_uid": "owner-1"}
        )
        assert run.status_code == 200, run.text
        data = run.json()
        assert "25%" in data["corrupted_text"]
        checks = {c["check"]: c["verdict"] for c in data["guardian_report"]["checks"]}
        assert checks["numeric_parity"] == "FAIL"
        assert data["original_unchanged"] is True
        # Original untouched: refetch the campaign assets.
        assets_after = [a for a in client.get(f"/api/campaigns/{campaign_id}/assets").json() if a["id"] == target["id"]]
        assert assets_after[0]["text_content"] == data["original_text"]
    finally:
        monkeypatch.setenv("TITAN_ENABLE_DEMO_SABOTAGE", "false")
        config.reset_settings_cache()


def test_scene_labels_on_one_line_are_not_numbers():
    """The live model writes every scene on one line; labels are still metadata."""
    from app.guardian.checks_text import verify_text

    tokens = {"DISCOUNT": "20%", "DAYS": "Saturday & Sunday"}
    script = "Scene 1: Iced drinks on the counter. Scene 2: Friends laughing. Scene 3: A toast."
    outcomes = {o.check: o for o in verify_text(script, tokens)}
    assert outcomes["numeric_parity"].verdict == "PASS"
    assert len(outcomes["numeric_parity"].details["scene_labels_ignored"]) == 3

    # A fabricated number inside a scene line still fails.
    outcomes = {o.check: o for o in verify_text(script + " Scene 4: 50% off sign.", tokens)}
    assert outcomes["numeric_parity"].verdict == "FAIL"
    assert outcomes["numeric_parity"].details["unmapped_numbers"] == ["50"]
