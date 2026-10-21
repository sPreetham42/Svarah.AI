import subprocess

import pytest

from app.services import sarvam
from app.services.video_service import _resolved_ffmpeg


@pytest.mark.skipif(_resolved_ffmpeg() is None, reason="FFmpeg not available")
def test_webm_recording_becomes_wav_and_garbage_is_left_alone():
    webm = subprocess.run(
        [_resolved_ffmpeg(), "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "libopus", "-f", "webm", "pipe:1"],
        capture_output=True, check=True,
    ).stdout
    wav, mime = sarvam._normalize_to_wav(webm)
    assert mime == "audio/wav" and wav[:4] == b"RIFF"
    assert sarvam._normalize_to_wav(b"not audio") is None
