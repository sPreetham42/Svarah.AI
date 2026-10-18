"""Phase 6 video tests: job lifecycle, FFmpeg output validity, budgets."""

from __future__ import annotations

import base64
import io
import shutil
import wave

import pytest

#: The local reel composer shells out to FFmpeg; without the binaries these
#: tests cannot exercise it (the job then fails honestly with ffmpeg_unavailable).
needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are not installed on this machine",
)


def _wav_b64() -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x01" * 16000)
    return base64.b64encode(buffer.getvalue()).decode()


def _full_setup(client) -> tuple[int, dict]:
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
    profile = client.post(
        "/api/voice-profiles",
        json={
            "owner_uid": "owner-1",
            "display_name": "Shopkeeper",
            "consent_confirmed": True,
            "reference_audio_b64": _wav_b64(),
            "reference_audio_mime": "audio/wav",
        },
    ).json()
    voice_asset = client.post(
        f"/api/campaigns/{campaign_id}/voice",
        json={"profile_id": profile["id"], "owner_uid": "owner-1", "language": "English"},
    ).json()
    return campaign_id, {"profile_id": profile["id"], "voice_asset_id": voice_asset["id"]}


def test_video_job_requires_poster(client):
    response = client.post(
        "/api/campaigns",
        json={"text": "20% off cold coffee this Saturday and Sunday, 4 PM to 8 PM at our cafe."},
    )
    campaign_id = response.json()["id"]
    sheet_id = response.json()["factsheet"]["id"]
    client.post(f"/api/factsheets/{sheet_id}/lock")
    client.post(f"/api/campaigns/{campaign_id}/plan")
    response = client.post(
        f"/api/campaigns/{campaign_id}/videos/jobs", json={"owner_uid": "owner-1"}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "poster_missing"


@needs_ffmpeg
def test_video_job_full_lifecycle(client):
    campaign_id, _ = _full_setup(client)
    queued = client.post(
        f"/api/campaigns/{campaign_id}/videos/jobs", json={"owner_uid": "owner-1", "profile_id": None}
    )
    assert queued.status_code == 202, queued.text
    job = queued.json()
    assert job["status"] == "queued"

    executed = client.post(f"/api/campaigns/videos/jobs/{job['id']}/run")
    assert executed.status_code == 200, executed.text
    data = executed.json()
    assert data["job"]["status"] == "completed"

    assets = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    videos = [a for a in assets if a["kind"] == "video"]
    assert len(videos) == 1
    reel = videos[0]
    assert reel["sha256"].startswith("sha256:")
    assert reel["provider"] == "ffmpeg"
    # Voice provenance must be preserved into the reel.
    assert reel["provenance"]["voice_asset_id"] is not None
    assert reel["provenance"]["voice_is_mock"] is True


def test_video_budget_cap(client):
    campaign_id, _ = _full_setup(client)
    for _ in range(6):
        queued = client.post(
            f"/api/campaigns/{campaign_id}/videos/jobs", json={"owner_uid": "owner-1"}
        )
        if queued.status_code == 409:
            break
        job = queued.json()
        client.post(f"/api/campaigns/videos/jobs/{job['id']}/run")
    assets = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert len([a for a in assets if a["kind"] == "video"]) <= 2


@needs_ffmpeg
def test_probe_rejects_invalid_media():
    from pathlib import Path

    from app.services.video_service import probe_video

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        bogus = Path(tmp) / "bogus.mp4"
        bogus.write_bytes(b"not a video at all")
        try:
            probe_video(bogus)
            raise AssertionError("expected media_invalid")
        except Exception as exc:
            assert getattr(exc, "code", "") == "media_invalid"


def test_reel_job_fails_honestly_without_ffmpeg(client, monkeypatch):
    """No FFmpeg → a failed job with the reason; never a placeholder video."""
    from app.services import video_service

    monkeypatch.setattr(video_service, "_resolved_ffmpeg", lambda: None)
    campaign_id, _ = _full_setup(client)
    job = client.post(f"/api/campaigns/{campaign_id}/videos/jobs", json={}).json()
    result = client.post(f"/api/campaigns/videos/jobs/{job['id']}/run").json()
    assert result["asset_id"] is None
    assert result["job"]["status"] == "failed"
    assert "FFmpeg" in result["job"]["error"]
    assets = client.get(f"/api/campaigns/{campaign_id}/assets").json()
    assert [a for a in assets if a["kind"] == "video"] == []
