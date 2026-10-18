"""Live provider smoke test — makes REAL, billable Agnes and Sarvam calls.

Run from `backend/` with credentials in the repository-root `.env`:

    .venv/Scripts/python scripts/smoke_live.py              # chat, TTS, STT, image
    .venv/Scripts/python scripts/smoke_live.py --video      # also one 4 s 720P video
    .venv/Scripts/python scripts/smoke_live.py --video-only # just the video check

It forces `TITAN_MODE=live` for this process only (your `.env` is not changed)
and exercises the same client classes the API uses. Each check prints PASS or
FAIL with the normalized error code; keys are never printed. The speech check
is a round trip: Sarvam TTS speaks a sentence, Sarvam STT transcribes it back.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
import wave
from pathlib import Path

# `app` lives one folder up (backend/), wherever this script is started from.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["TITAN_MODE"] = "live"
# Provider messages may contain characters a Windows console cannot encode.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

from app.config import get_settings  # noqa: E402
from app.errors import TitanError  # noqa: E402
from app.services import agnes, sarvam  # noqa: E402

SENTENCE = "Twenty percent off cold coffee this Saturday and Sunday."
results: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)


async def check(name: str, coro):
    try:
        detail = await coro
    except TitanError as exc:
        record(name, False, f"{exc.code}: {exc.message}")
        return None
    except Exception as exc:  # noqa: BLE001 - a smoke test reports everything
        record(name, False, f"{type(exc).__name__}: {exc}")
        return None
    record(name, True, detail[0] if isinstance(detail, tuple) else detail)
    return detail


async def chat(settings):
    client = agnes.get_llm_client(settings)
    result = await client.complete_json(
        "Return ONLY a JSON object.", 'Reply with {"ok": true, "word": "namaste"}.', max_tokens=60
    )
    parsed = agnes.parse_llm_json(result)
    if parsed.get("ok") is not True:
        raise RuntimeError(f"unexpected JSON: {parsed}")
    return f"{result.model} returned valid JSON"


async def tts(settings):
    client = sarvam.get_tts_client(settings)
    result = await client.synthesize(SENTENCE, language_code="en-IN", speaker=sarvam.TTS_DEFAULT_SPEAKER)
    with wave.open(io.BytesIO(result.audio), "rb") as reader:
        seconds = reader.getnframes() / float(reader.getframerate())
    if seconds <= 0.5:
        raise RuntimeError(f"audio too short ({seconds:.2f}s)")
    return f"{result.model} produced {seconds:.1f}s of WAV ({len(result.audio)} bytes)", result.audio


async def stt(settings, audio: bytes):
    client = sarvam.get_stt_sarvam(settings)
    result = await client.transcribe(audio, mime="audio/wav", language_hint="en")
    if "coffee" not in result.transcript.lower():
        raise RuntimeError(f"transcript does not match the spoken sentence: {result.transcript!r}")
    return f"{settings.sarvam_stt_model} transcribed: {result.transcript!r}"


async def image(settings):
    from app.services.poster import NO_TEXT_SUFFIX, _load_model_art, _validated_art

    client = agnes.get_image_client(settings)
    result = await client.generate_art(
        "Iced coffee on a small cafe counter, warm light." + NO_TEXT_SUFFIX, size="1K", ratio="3:4"
    )
    data = _validated_art(await asyncio.to_thread(_load_model_art, result))
    return f"{result.model} returned a valid image ({len(data)} bytes)"


async def video(settings):
    client = agnes.get_video_client(settings)
    task = await client.create(
        "Slow push-in on an iced coffee on a cafe counter, warm light. No text.",
        seconds=4, size="720P", aspect_ratio="9:16",
    )
    print(f"       video task accepted ({task.status}); polling…", flush=True)
    deadline = time.time() + 12 * 60
    while task.status not in (agnes.VIDEO_STATUS_COMPLETED, agnes.VIDEO_STATUS_FAILED):
        if time.time() > deadline:
            raise RuntimeError(f"still {task.status} after 12 minutes (task {task.video_id})")
        await asyncio.sleep(5)
        task = await client.retrieve(task.video_id)
        print(f"       {task.status} {task.progress if task.progress is not None else ''}", flush=True)
    if task.status == agnes.VIDEO_STATUS_FAILED:
        raise RuntimeError(f"provider reported failure: {task.error}")
    if not task.url:
        raise RuntimeError("completed without a video URL")
    data = await client.download(task.url, max_bytes=200 * 1024 * 1024)
    if data[4:8] != b"ftyp" and data[:4] != b"\x1aE\xdf\xa3":
        raise RuntimeError("downloaded file is not a recognisable video")
    return f"{task.model} produced a {len(data)} byte video"


async def main() -> int:
    settings = get_settings()
    print(f"mode={settings.titan_mode} agnes_configured={settings.agnes_configured} "
          f"sarvam_configured={settings.sarvam_configured}")
    if "--video-only" not in sys.argv:
        await check("Agnes chat (text model)", chat(settings))
        spoken = await check("Sarvam TTS", tts(settings))
        if spoken:
            await check("Sarvam STT (round trip of the TTS audio)", stt(settings, spoken[1]))
        else:
            record("Sarvam STT (round trip of the TTS audio)", False, "skipped: no TTS audio to transcribe")
        await check("Agnes image", image(settings))
    if "--video-only" in sys.argv:
        results.clear()
    if "--video" in sys.argv or "--video-only" in sys.argv:
        await check("Agnes video", video(settings))
    else:
        print("[SKIP] Agnes video: pass --video to run (billable, takes minutes)")
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
