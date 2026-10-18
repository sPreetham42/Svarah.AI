"""Live transcription preview (WebSocket).

This is a **preview**, not the authoritative transcript. The campaign pipeline
still transcribes the finished clip, so nothing downstream — extraction, the
fact lock, Guardian — ever depends on a partial. That keeps the integrity rules
exactly as they were while giving the speaker their words back as they talk.

Mock mode streams the same deterministic text the offline provider returns, so
the preview and the saved transcript agree. Live mode transcribes for real:
while the speaker talks, the audio received so far is sent to the configured
speech-to-text provider about once a second and each result is pushed back as
a partial. If the provider cannot be reached the socket says so once and the
client simply keeps recording; on release the normal pipeline still produces
the real transcript.

Auth: the token arrives in the first message, never in the query string, so a
credential cannot leak through a URL or an access log.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from ..config import get_settings
from ..db import get_session_factory
from ..errors import ProviderUnavailableError, STTUnavailableError, TitanError
from ..models import User
from ..security import read_token
from ..services.stt import MockSTTProvider, get_stt_provider

router = APIRouter(tags=["stt"])

#: How often a mock partial is revealed, in seconds.
PARTIAL_INTERVAL = 0.5

#: Audio the preview is willing to buffer before refusing more (the real upload
#: path enforces its own limit; this is only a backstop).
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: Live mode: pause between two transcriptions of the growing clip, and the
#: least audio worth sending (a WebM header alone is not speech).
LIVE_INTERVAL = 0.9
LIVE_MIN_BYTES = 6_000
#: Consecutive provider failures tolerated before the preview gives up.
LIVE_MAX_ERRORS = 3

#: Seconds to wait for the auth message before giving up.
AUTH_TIMEOUT = 10.0


async def _authenticate(websocket: WebSocket) -> bool:
    """Validate the first message's token. Returns False after closing."""
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await _safe_close(websocket, 4401)
        return False
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        await _send(websocket, {"type": "error", "code": "bad_message", "message": "Expected a JSON auth message."})
        await _safe_close(websocket, 4401)
        return False
    if payload.get("type") != "auth" or not payload.get("token"):
        await _send(websocket, {"type": "error", "code": "auth_required", "message": "Sign in to stream audio."})
        await _safe_close(websocket, 4401)
        return False

    settings = get_settings()
    try:
        claims = read_token(str(payload["token"]), settings)
    except TitanError as exc:
        await _send(websocket, {"type": "error", "code": getattr(exc, "code", "token_invalid"), "message": str(exc)})
        await _safe_close(websocket, 4401)
        return False

    with get_session_factory()() as session:
        user = session.scalar(select(User).where(User.owner_uid == claims.uid))
    if user is None:
        await _send(websocket, {"type": "error", "code": "account_missing", "message": "This account no longer exists."})
        await _safe_close(websocket, 4401)
        return False
    return True


async def _send(websocket: WebSocket, payload: dict) -> None:
    try:
        await websocket.send_json(payload)
    except (RuntimeError, WebSocketDisconnect):
        pass


async def _safe_close(websocket: WebSocket, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except RuntimeError:
        pass


async def _stream_partials(websocket: WebSocket, words: list[str]) -> None:
    """Reveal the transcript word by word, the way a streaming provider does."""
    try:
        for index in range(1, len(words) + 1):
            await asyncio.sleep(PARTIAL_INTERVAL)
            await websocket.send_json(
                {"type": "partial", "text": " ".join(words[:index]), "is_mock": True}
            )
    except (asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
        return


async def _live_partials(
    websocket: WebSocket, buffer: bytearray, state: dict, *, mime: str, language: str | None
) -> None:
    """Re-transcribe the clip so far, about once a second, with the real provider."""
    try:
        provider = get_stt_provider(get_settings())
    except TitanError as exc:
        await _send(websocket, {"type": "unavailable", "reason": getattr(exc, "message", str(exc))})
        return
    errors = 0
    try:
        while True:
            await asyncio.sleep(LIVE_INTERVAL)
            size = len(buffer)
            if size < LIVE_MIN_BYTES or size <= state["transcribed"]:
                continue
            try:
                result = await provider.transcribe(bytes(buffer), mime=mime, language_hint=language)
            except (STTUnavailableError, ProviderUnavailableError) as exc:
                errors += 1
                if errors >= LIVE_MAX_ERRORS:
                    await _send(
                        websocket,
                        {"type": "unavailable", "reason": getattr(exc, "message", str(exc))},
                    )
                    return
                continue
            errors = 0
            state["transcribed"] = size
            text = (result.raw_transcript or "").strip()
            if text and text != state["text"]:
                state["text"] = text
                state["provider"] = result.provider
                await _send(
                    websocket,
                    {"type": "partial", "text": text, "is_mock": False, "provider": result.provider},
                )
    except (asyncio.CancelledError, RuntimeError, WebSocketDisconnect):
        return


async def _live_session(websocket: WebSocket, language: str | None) -> None:
    mime = websocket.query_params.get("mime") or "audio/webm"
    buffer = bytearray()
    state = {"transcribed": 0, "text": "", "provider": None}
    task = asyncio.create_task(_live_partials(websocket, buffer, state, mime=mime, language=language))
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk:
                if len(buffer) + len(chunk) > MAX_AUDIO_BYTES:
                    await _send(websocket, {"type": "error", "code": "request_too_large", "message": "That recording is too long."})
                    break
                buffer.extend(chunk)
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if control.get("type") == "stop":
                break
    except WebSocketDisconnect:
        return
    finally:
        task.cancel()
    # The finished clip is transcribed again by the campaign pipeline; this is
    # only the last preview the speaker saw.
    await _send(
        websocket,
        {"type": "final", "text": state["text"], "is_mock": False, "provider": state["provider"], "language": language},
    )
    await _safe_close(websocket)


@router.websocket("/stt/stream")
async def stt_stream(websocket: WebSocket) -> None:
    settings = get_settings()
    language = websocket.query_params.get("language") or None

    await websocket.accept()
    if not await _authenticate(websocket):
        return

    await _send(
        websocket,
        {
            "type": "ready",
            "streaming": True,
            "is_mock": settings.is_mock,
            "provider": "mock" if settings.is_mock else settings.stt_provider,
        },
    )

    if not settings.is_mock:
        await _live_session(websocket, language)
        return

    text = MockSTTProvider.DEFAULT_TEXT
    task = asyncio.create_task(_stream_partials(websocket, text.split()))
    buffered = 0
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk:
                buffered += len(chunk)
                if buffered > MAX_AUDIO_BYTES:
                    await _send(websocket, {"type": "error", "code": "request_too_large", "message": "That recording is too long."})
                    break
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if control.get("type") == "stop":
                break
    except WebSocketDisconnect:
        return
    finally:
        task.cancel()

    await _send(
        websocket,
        {
            "type": "final",
            "text": text,
            "is_mock": True,
            "provider": "mock",
            "language": language or "en",
        },
    )
    await _safe_close(websocket)
