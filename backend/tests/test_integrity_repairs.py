"""Integrity-repair regression tests.

Covers the confirmed defects from the audit:

1. full SHA-256 checksums everywhere (no false tamper on untouched audio);
2. the Guardian's persisted aggregate verdict is authoritative for certificates
   (a genuine FAIL cannot be hidden by a later PASS row);
3. failed/unverified/stale assets cannot be prepared, approved or published;
4. ASR actually runs and reports mock/unavailable states honestly;
5. internal scene metadata passes while fabricated business numbers still fail.
"""

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


def _wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 16000)
    return buffer.getvalue()


def _setup_full(client, *, fabricate_caption: bool = False) -> int:
    response = client.post(
        "/api/campaigns",
        json={
            "text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe for college students."
        },
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.patch(f"/api/factsheets/{sheet_id}", json={"languages": ["English"]})
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
            "reference_audio_b64": base64.b64encode(_wav()).decode(),
            "reference_audio_mime": "audio/wav",
        },
    ).json()
    client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "English"},
    )
    if fabricate_caption:
        from app.db import get_session_factory
        from app.models import AssetRecord

        details = client.get(f"/api/campaigns/{campaign_id}").json()["factsheet"]
        with get_session_factory()() as session:
            session.add(
                AssetRecord(
                    campaign_id=campaign_id,
                    factsheet_id=details["id"],
                    fact_hash=details["fact_hash"],
                    kind="caption",
                    locale="en-IN",
                    text_content="25% off cold coffee this Saturday & Sunday.",
                    asset_status="validating",
                    provider="test_fixture",
                )
            )
            session.commit()
    return campaign_id


def _assets_by_id(client, campaign_id: int) -> dict[int, dict]:
    return {a["id"]: a for a in client.get(f"/api/campaigns/{campaign_id}/assets").json()}


# ── 1. checksums ──────────────────────────────────────────────────────
def test_sha256_utility_is_full_digest():
    from app.services.checksums import sha256_digest, sha256_hex, sha256_text

    digest = sha256_digest(b"titan")
    assert digest.startswith("sha256:")
    assert len(digest) == len("sha256:") + 64
    assert sha256_hex(b"titan") == digest.removeprefix("sha256:")
    assert sha256_text("titan") == digest


def test_untouched_voice_asset_passes_checksum_and_only_real_tamper_fails(client):
    """The false-tamper regression: full SHA-256 at creation AND verification."""
    campaign_id = _setup_full(client)
    assert client.post(f"/api/campaigns/{campaign_id}/verify").status_code == 200

    voice = next(a for a in _assets_by_id(client, campaign_id).values() if a["kind"] == "voice")
    assert len(voice["sha256"]) == len("sha256:") + 64

    from app.guardian.certificate import build_certificate, certificate_valid
    from app.db import get_session_factory
    from app.models import AssetRecord
    from app.storage import get_storage

    with get_session_factory()() as session:
        certificate = build_certificate(session, campaign_id)
    voice_entry = next(a for a in certificate["assets"] if a["asset_id"] == voice["id"])
    assert voice_entry["bytes_unchanged"] is True
    assert voice_entry["current_sha256"] == voice_entry["sha256"]
    validity = certificate_valid(certificate)
    assert validity["assets_unchanged"] is True, validity["tampered"]
    assert validity["seal_valid"] is True

    # A real modification IS detected (and now forces a FAILED verdict).
    with get_session_factory()() as session:
        record = session.get(AssetRecord, voice["id"])
        get_storage().save_bytes(b"tampered-audio-bytes", record.storage_path)
    with get_session_factory()() as session:
        tampered_certificate = build_certificate(session, campaign_id)
    tampered_entry = next(a for a in tampered_certificate["assets"] if a["asset_id"] == voice["id"])
    assert tampered_entry["bytes_unchanged"] is False
    assert tampered_entry["current_sha256"] != tampered_entry["sha256"]
    assert tampered_certificate["verdict"] == "FAILED"
    assert certificate_valid(tampered_certificate)["tampered"][0]["reason"] == "bytes_changed"


# ── 2. authoritative verdicts ─────────────────────────────────────────
def test_certificate_reports_critical_failure_and_ignores_later_pass_rows(client):
    campaign_id = _setup_full(client, fabricate_caption=True)
    reports = {r["asset_id"]: r["verdict"] for r in client.post(f"/api/campaigns/{campaign_id}/verify").json()}
    failed_id = next(asset_id for asset_id, verdict in reports.items() if verdict == "FAIL")

    from app.db import get_session_factory
    from app.models import VerificationResultRecord
    from app.guardian.certificate import build_certificate
    from app.guardian.runner import AGGREGATE_CHECK

    with get_session_factory()() as session:
        aggregate_rows = [
            row
            for row in session.query(VerificationResultRecord)
            .filter(VerificationResultRecord.asset_id == failed_id)
            .all()
            if row.check_name == AGGREGATE_CHECK
        ]
        assert aggregate_rows, "runner must persist an aggregate verdict row"
        assert aggregate_rows[-1].verdict == "FAIL"

        # Simulate the old bug: append a later PASS row that must NOT win.
        session.add(
            VerificationResultRecord(
                asset_id=failed_id,
                check_name="numeric_parity",
                verdict="PASS",
                confidence=1.0,
                method="deterministic",
                details_json="{}",
                attempt=1,
            )
        )
        session.commit()

    with get_session_factory()() as session:
        certificate = build_certificate(session, campaign_id)
    entry = next(a for a in certificate["assets"] if a["asset_id"] == failed_id)
    assert entry["verdict"] == "FAIL"
    assert entry["asset_status"] == "failed"
    assert certificate["verdict"] == "FAILED"


# ── 3. publishing gate ────────────────────────────────────────────────
def test_publishing_gate_rejects_every_failed_asset_path(client):
    campaign_id = _setup_full(client, fabricate_caption=True)
    client.post(f"/api/campaigns/{campaign_id}/verify")
    assets = _assets_by_id(client, campaign_id)
    failed = next(a for a in assets.values() if a["asset_status"] == "failed")

    prepare = client.post(
        f"/api/campaigns/{campaign_id}/publish/prepare",
        json={"asset_id": failed["id"], "platform": "whatsapp", "media_kind": "text", "owner_uid": "owner-1"},
    )
    assert prepare.status_code == 409, prepare.text
    assert prepare.json()["error"]["code"] == "asset_not_verified"

    from app.db import get_session_factory
    from app.models import AssetRecord

    verified_captions = [
        a for a in assets.values() if a["kind"] == "caption" and a["asset_status"] == "verified"
    ]
    assert len(verified_captions) >= 2

    def _prepare(asset_id: int) -> dict:
        response = client.post(
            f"/api/campaigns/{campaign_id}/publish/prepare",
            json={
                "asset_id": asset_id,
                "platform": "whatsapp",
                "media_kind": "text",
                "owner_uid": "owner-1",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _mark_failed(asset_id: int) -> None:
        with get_session_factory()() as session:
            record = session.get(AssetRecord, asset_id)
            record.asset_status = "failed"
            session.commit()

    # (b) Gate re-checked at APPROVAL: verified at prepare, failed before approve.
    approve_target = verified_captions[0]
    pending = _prepare(approve_target["id"])
    _mark_failed(approve_target["id"])
    approved = client.post(f"/api/campaigns/publish/{pending['id']}/approve")
    assert approved.status_code == 409, approved.text
    assert approved.json()["error"]["code"] == "asset_not_verified"

    # (c) Gate re-checked at EXECUTION: approved while verified, failed afterwards.
    execute_target = verified_captions[1]
    record = _prepare(execute_target["id"])
    assert client.post(f"/api/campaigns/publish/{record['id']}/approve").status_code == 200
    _mark_failed(execute_target["id"])
    executed = client.post(f"/api/campaigns/publish/{record['id']}/execute")
    assert executed.status_code == 409, executed.text
    assert executed.json()["error"]["code"] == "asset_not_verified"


# ── 4. ASR ────────────────────────────────────────────────────────────
def test_asr_runs_and_discloses_mock_transcript(client):
    """The ASR leg must actually execute; mock transcripts are disclosed, not skipped."""
    campaign_id = _setup_full(client)
    reports = {r["asset_id"]: r for r in client.post(f"/api/campaigns/{campaign_id}/verify").json()}
    voice = next(a for a in _assets_by_id(client, campaign_id).values() if a["kind"] == "voice")
    checks = {c["check"]: c for c in reports[voice["id"]]["checks"]}
    asr = checks["asr_roundtrip"]
    assert asr["verdict"] == "NEEDS_REVIEW"
    assert "mock STT provider" in asr["details"]["note"]
    assert asr["details"]["provider"] == "mock"
    assert asr["details"]["audio_bytes"] > 0


class _FakeSttProvider:
    name = "fake_live"

    def __init__(self, transcript: str | None = None, error: Exception | None = None) -> None:
        self._transcript = transcript
        self._error = error

    async def transcribe(self, audio, *, mime=None, language_hint=None):
        if self._error is not None:
            raise self._error
        from app.services.stt import TranscriptResult

        return TranscriptResult(
            raw_transcript=self._transcript or "", provider=self.name, is_mock=False
        )


def _patch_stt(monkeypatch, provider):
    from app.services import stt

    monkeypatch.setattr(stt, "get_stt_provider", lambda settings=None: provider)


def test_asr_passes_with_a_live_transcript_that_matches(monkeypatch):
    from app.services.asr import roundtrip_check

    _patch_stt(monkeypatch, _FakeSttProvider("20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM."))
    outcome = roundtrip_check(b"audio-bytes", "script", TOKENS, language_code="en-IN")
    assert outcome.verdict == "PASS"
    assert outcome.details["provider"] == "fake_live"


def test_asr_fails_when_the_transcript_mutates_a_locked_fact(monkeypatch):
    from app.services.asr import roundtrip_check

    _patch_stt(monkeypatch, _FakeSttProvider("25% off cold coffee this Saturday and Sunday."))
    outcome = roundtrip_check(b"audio-bytes", "script", TOKENS, language_code="en-IN")
    assert outcome.verdict == "FAIL"
    assert "numeric_parity" in outcome.details["failed_checks"]


def test_asr_reports_unavailable_honestly(monkeypatch):
    from app.errors import STTUnavailableError
    from app.services.asr import roundtrip_check

    _patch_stt(monkeypatch, _FakeSttProvider(error=STTUnavailableError("ASR is disabled for this test.")))
    outcome = roundtrip_check(b"audio-bytes", "script", TOKENS, language_code="en-IN")
    assert outcome.verdict == "NEEDS_REVIEW"
    assert "unavailable" in outcome.details["note"]


# ── 5. scene metadata vs fabricated claims ────────────────────────────
def test_scene_labels_are_metadata_but_scene_numbers_still_fail(client):
    from app.guardian.checks_text import verify_text

    reel_script = (
        "Scene 1: cold coffee on display.\n"
        "Scene 2: Sign shows the offer.\n"
        "Scene 3: Happy college students.\n"
        "Scene 4: End card — Saturday & Sunday 4 PM–8 PM."
    )
    report = {o.check: o for o in verify_text(reel_script, TOKENS)}
    assert all(o.verdict == "PASS" for o in report.values()), {
        k: (o.verdict, o.details) for k, o in report.items()
    }
    assert report["numeric_parity"].details["scene_labels_ignored"] == [
        "Scene 1:",
        "Scene 2:",
        "Scene 3:",
        "Scene 4:",
    ]

    # Fabricated business numbers INSIDE scene lines still fail.
    report = {o.check: o.verdict for o in verify_text("Scene 1: 25% off cold coffee!", dict(TOKENS))}
    assert report["numeric_parity"] == "FAIL"
    report = {o.check: o.verdict for o in verify_text("Scene 3: price 999 rupees only!", dict(TOKENS))}
    assert report["numeric_parity"] == "FAIL"
    report = {o.check: o.verdict for o in verify_text("Shot 2 - 30% off everything!", dict(TOKENS))}
    assert report["numeric_parity"] == "FAIL"
    # Claims inside scene lines are still claims.
    report = {o.check: o.verdict for o in verify_text("Scene 1: Best in town!", dict(TOKENS))}
    assert report["unsupported_claims"] == "FAIL"


def test_asset_edit_invalidates_the_certificate(client):
    """A human edit after issuance must invalidate the certificate, not hide."""
    campaign_id = _setup_full(client)
    assert client.post(f"/api/campaigns/{campaign_id}/verify").status_code == 200
    caption = next(
        a
        for a in _assets_by_id(client, campaign_id).values()
        if a["kind"] == "caption" and a["asset_status"] == "verified"
    )

    from app.db import get_session_factory
    from app.guardian.certificate import build_certificate, certificate_valid
    from app.models import AssetRecord

    with get_session_factory()() as session:
        before = build_certificate(session, campaign_id)
    entry = next(a for a in before["assets"] if a["asset_id"] == caption["id"])
    assert entry["bytes_unchanged"] is True  # no false tamper, even for text assets
    assert certificate_valid(before)["assets_unchanged"] is True

    with get_session_factory()() as session:
        record = session.get(AssetRecord, caption["id"])
        record.text_content = "30% off cold coffee this Saturday & Sunday."  # human edit
        session.commit()

    with get_session_factory()() as session:
        after = build_certificate(session, campaign_id)
    edited = next(a for a in after["assets"] if a["asset_id"] == caption["id"])
    assert edited["bytes_unchanged"] is False
    assert edited["current_sha256"] != edited["sha256"]
    assert after["verdict"] == "FAILED"
    assert certificate_valid(after)["tampered"][0]["reason"] == "bytes_changed"


def test_mock_reel_script_verifies_clean_after_the_contract_fix(client):
    """The generated reel script itself must pass numeric parity (the old failure)."""
    campaign_id = _setup_full(client)
    reports = {r["asset_id"]: r["verdict"] for r in client.post(f"/api/campaigns/{campaign_id}/verify").json()}
    reel = next(
        a
        for a in _assets_by_id(client, campaign_id).values()
        if a["kind"] == "caption" and "Scene 1:" in (a["text_content"] or "")
    )
    assert reports[reel["id"]] == "PASS"
    assert reel["asset_status"] == "verified"
