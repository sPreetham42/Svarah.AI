"""STT provider, audio upload and graceful fallback tests."""

from __future__ import annotations

import asyncio
import base64
import importlib.util

import pytest

from app.errors import STTUnavailableError
from app.services.stt import (
    FasterWhisperSTTProvider,
    MockSTTProvider,
    get_stt_provider,
    normalize_transcript,
)

FAKE_AUDIO = b"\x1aE\xdf\xa3fake-webm-audio-bytes"
FAKE_AUDIO_B64 = base64.b64encode(FAKE_AUDIO).decode()


def test_normalize_transcript_collapses_whitespace():
    raw = "  Hello   world\n\ttabs  "
    assert normalize_transcript(raw) == "Hello world tabs"


def test_mock_provider_returns_labelled_transcript():
    provider = MockSTTProvider()
    result = asyncio.run(provider.transcribe(FAKE_AUDIO, mime="audio/webm"))
    assert result.is_mock is True
    assert result.provider == "mock"
    assert result.raw_transcript
    assert result.transcript_hash.startswith("sha256:")
    assert result.segments


def test_mock_mode_audio_upload_creates_campaign(client):
    response = client.post(
        "/api/campaigns",
        json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"},
    )
    assert response.status_code == 201
    body = response.json()

    assert body["input_type"] == "audio"
    assert body["stt"]["status"] == "ok"
    assert body["stt"]["provider"] == "mock"
    assert body["transcript"]["is_mock"] is True
    assert body["transcript"]["provider"] == "mock"
    assert body["audio_path"].endswith("audio.webm")

    # Raw audio is preserved on disk.
    from app.storage import get_storage

    assert get_storage().exists(body["audio_path"])
    assert get_storage().read_bytes(body["audio_path"]) == FAKE_AUDIO


def test_stt_disabled_degrades_to_typed_fallback(client, env_override):
    env_override(TITAN_STT_PROVIDER="none")
    response = client.post(
        "/api/campaigns",
        json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"},
    )
    assert response.status_code == 201
    body = response.json()

    # Campaign + audio survive; no transcript is invented.
    assert body["transcript"] is None
    assert body["stt"]["status"] == "unavailable"
    assert body["stt"]["fallback"] == "typed_text"
    assert body["audio_path"]

    event_types = [e["event_type"] for e in body["audit_events"]]
    assert "stt.unavailable" in event_types


def test_stt_provider_failure_is_graceful(client, monkeypatch):
    async def _explode(*args, **kwargs):
        raise STTUnavailableError("boom", details={"reason": "test"})

    monkeypatch.setattr("app.api.campaigns.get_stt_provider", lambda settings: _ExplodingSTT())

    class _ExplodingSTT:  # noqa: D401
        name = "exploding"

        async def transcribe(self, *args, **kwargs):
            await _explode()

    response = client.post(
        "/api/campaigns",
        json={"audio_b64": FAKE_AUDIO_B64, "audio_mime": "audio/webm"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["transcript"] is None
    assert body["stt"]["status"] == "unavailable"
    assert body["stt"]["fallback"] == "typed_text"


def test_faster_whisper_lazy_load_fails_cleanly_when_absent():
    if importlib.util.find_spec("faster_whisper") is not None:
        pytest.skip("faster-whisper is installed in this environment")
    provider = FasterWhisperSTTProvider("tiny", "int8")
    assert provider._model is None  # nothing loaded until first use
    with pytest.raises(STTUnavailableError):
        asyncio.run(provider.transcribe(FAKE_AUDIO))


def test_provider_selection_respects_mode(client, env_override):
    from app.config import get_settings

    env_override(TITAN_MODE="mock", TITAN_STT_PROVIDER="auto")
    assert isinstance(get_stt_provider(get_settings()), MockSTTProvider)
