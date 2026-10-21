"""Sarvam AI clients: Saaras STT, Bulbul TTS and voice cloning.

Contract verified against https://docs.sarvam.ai:

- STT:  POST {base}/speech-to-text — multipart ``file``, ``model``
        (``saaras:v4``), optional ``language_code`` (``unknown`` auto-detects);
        auth header ``api-subscription-key``; response ``transcript``,
        ``language_code``, ``request_id``. The synchronous endpoint is meant
        for short (< 30 s) clips. Partial/streaming transcription is a separate
        WebSocket API that this backend does not use, so the UI reports
        recording → processing → transcript rather than fake partials.
- TTS:  POST {base}/text-to-speech — JSON ``text`` (≤ 2 500 chars),
        ``language_code``, ``speaker``, ``model`` (``bulbul:v3``),
        ``output_audio_codec``; response ``audios`` (base64 WAV, one per input).
- Voice create/clone: POST {base}/voices/create and /voices/clone (multipart).

Up to four keys may be configured (``TITAN_SARVAM_API_KEY``, ``_2`` … ``_4``).
Every call starts with the key that last worked and moves to the next one
when a key is rejected, out of credits or rate limited.

Sarvam is the only speech provider. In ``TITAN_MODE=live`` these clients are
used directly and a missing key or a provider failure is surfaced as an error
— never replaced by a mock or another speech service. Offline mode uses
deterministic mocks labelled ``is_mock=True``.
"""

from __future__ import annotations

import abc
import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError, STTUnavailableError
from .gateway import ProviderError, with_retry

T = TypeVar("T")

#: Retained so older .env files keep loading; it no longer gates live calls.
SARVAM_VERIFY_KEY = "TITAN_SARVAM_INTERFACE_VERIFIED"

SUPPORTED_LANGUAGES: dict[str, str] = {
    "English": "en-IN",
    "Hindi": "hi-IN",
    "Kannada": "kn-IN",
    "Tamil": "ta-IN",
    "Telugu": "te-IN",
    "Bengali": "bn-IN",
    "Gujarati": "gu-IN",
    "Malayalam": "ml-IN",
    "Marathi": "mr-IN",
    "Odia": "od-IN",
    "Punjabi": "pa-IN",
}
LANGUAGE_NAME_BY_CODE = {code: name for name, code in SUPPORTED_LANGUAGES.items()}

MAX_CLONE_TEXT_CHARS = 1000  # documented request limit; sentence-split above this

# ── TTS (Bulbul) — documented values only ─────────────────────────────
TTS_MODEL = "bulbul:v3"
MAX_TTS_TEXT_CHARS = 2500
TTS_DEFAULT_SPEAKER = "shubh"
#: Speakers documented for bulbul:v3 (lower-case, case-sensitive).
TTS_SPEAKERS: tuple[str, ...] = (
    "shubh", "aditya", "ritu", "priya", "neha", "rahul", "pooja", "rohan",
    "simran", "kavya", "amit", "dev", "ishita", "shreya", "ratan", "varun",
    "manan", "sumit", "roopa", "kabir", "aayan", "ashutosh", "advait", "anand",
    "tanya", "tarun", "sunny", "mani", "gokul", "vijay", "shruti", "suhani",
    "mohit", "kavitha", "rehan", "soham", "rupali",
)
#: Language codes documented for the TTS endpoint.
TTS_LANGUAGE_CODES: tuple[str, ...] = (
    "bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN",
    "pa-IN", "ta-IN", "te-IN",
)

NOT_CONFIGURED_MESSAGE = (
    "Sarvam is not configured: set TITAN_SARVAM_API_KEY (or run with TITAN_MODE=mock)."
)


def sarvam_live_allowed(settings: Settings) -> bool:
    """Live Sarvam calls need live mode and a configured key."""
    return bool(not settings.is_mock and settings.sarvam_configured)


#: Failures tied to one key: rejected or revoked, out of credits, or that key's
#: rate limit. Another configured key may succeed. Any other failure is about
#: the request or the provider, and switching keys would not help.
_KEY_SCOPED_CODES = frozenset({"provider_auth_error", "provider_quota_exceeded", "provider_rate_limited"})
_KEY_SCOPED_STATUSES = frozenset({401, 402, 403, 429})

#: The key that last succeeded. Calls start from it, so an exhausted key is not
#: tried first on every request.
_last_good_key: str | None = None


def _ordered_keys(settings: Settings) -> list[str]:
    keys = list(settings.sarvam_api_keys)
    if _last_good_key in keys:
        start = keys.index(_last_good_key)
        keys = keys[start:] + keys[:start]
    return keys


async def _with_key_failover(
    settings: Settings,
    provider: str,
    call: Callable[[dict[str, str]], Awaitable[T]],
) -> T:
    """Run ``call(headers)`` with each configured key until one is accepted."""
    global _last_good_key
    keys = _ordered_keys(settings)
    if not keys:
        raise ProviderError(
            NOT_CONFIGURED_MESSAGE, provider=provider, code="provider_not_configured", retryable=False
        )
    last: ProviderError | None = None
    for key in keys:
        try:
            result = await call({"api-subscription-key": key})
        except ProviderError as exc:
            if exc.code not in _KEY_SCOPED_CODES and exc.status_code not in _KEY_SCOPED_STATUSES:
                raise
            last = exc
            continue
        _last_good_key = key
        return result
    assert last is not None
    if len(keys) > 1:
        last.message = f"All {len(keys)} Sarvam keys failed. Last error: {last.message}"
        last.args = (last.message,)
    raise last


def _attempt_timeout(settings: Settings, per_call: float) -> float:
    """Budget for one sweep over the keys (rejected keys answer quickly)."""
    return per_call + 5.0 * max(1, len(settings.sarvam_api_keys))


def _base_url(settings: Settings) -> str:
    return (settings.sarvam_api_base or "https://api.sarvam.ai").rstrip("/")


def _sarvam_error(response: httpx.Response, provider: str, *, what: str) -> ProviderError | None:
    """Map a Sarvam HTTP status + error body to a normalized error.

    Sarvam returns ``{"error": {"message", "code", "request_id"}}``. Auth,
    quota and validation errors are permanent and must not be retried.
    """
    status = response.status_code
    if status < 400:
        return None
    message = ""
    provider_code = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = str(body["error"].get("message") or "")
        provider_code = str(body["error"].get("code") or "")
    suffix = f": {message[:300]}" if message else "."

    if status in (401, 403) or provider_code in ("invalid_api_key_error", "authentication_error"):
        return ProviderError(
            f"Sarvam rejected the API key ({status}){suffix}",
            provider=provider, code="provider_auth_error", retryable=False, status_code=status,
        )
    if provider_code == "insufficient_quota_error":
        return ProviderError(
            f"The Sarvam account is out of quota{suffix}",
            provider=provider, code="provider_quota_exceeded", retryable=False, status_code=status,
        )
    if status == 429:
        return ProviderError(
            "Sarvam rate limit reached. Try again in a minute.",
            provider=provider, code="provider_rate_limited", retryable=True, status_code=status,
        )
    if status >= 500:
        return ProviderError(
            f"Sarvam server error ({status}){suffix}",
            provider=provider, code="provider_server_error", retryable=True, status_code=status,
        )
    return ProviderError(
        f"Sarvam rejected the {what} ({status}){suffix}",
        provider=provider, code="provider_bad_request", retryable=False, status_code=status,
    )


def _sarvam_json(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError(
            "Sarvam returned a non-JSON response.",
            provider=provider, code="provider_bad_response", retryable=False,
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(
            "Sarvam returned an unexpected response shape.",
            provider=provider, code="provider_bad_response", retryable=False,
        )
    return body


#: Our UI speaks in short codes ("en", "hi"); Sarvam wants BCP-47 ("en-IN").
#: An unrecognised hint is dropped rather than sent, so the provider
#: auto-detects instead of rejecting the request.
_SARVAM_LANGUAGE_CODES = {
    "en": "en-IN", "hi": "hi-IN", "kn": "kn-IN", "ta": "ta-IN", "te": "te-IN",
    "ml": "ml-IN", "mr": "mr-IN", "bn": "bn-IN", "gu": "gu-IN", "pa": "pa-IN",
    "od": "od-IN", "or": "od-IN", "ur": "ur-IN", "as": "as-IN", "ne": "ne-IN",
}
#: Codes the speech-to-text endpoint documents.
_STT_LANGUAGE_CODES = frozenset(
    {
        "hi-IN", "bn-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN",
        "te-IN", "en-IN", "gu-IN", "as-IN", "ur-IN", "ne-IN", "kok-IN", "ks-IN",
        "sd-IN", "sa-IN", "sat-IN", "mni-IN", "brx-IN", "mai-IN", "doi-IN",
    }
)

#: Upload filename per container, so the provider can sniff the format.
_AUDIO_EXTENSIONS = {
    "audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/opus": "opus",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/aac": "aac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "audio/flac": "flac", "audio/amr": "amr", "audio/aiff": "aiff",
}


def sarvam_language_code(hint: str | None) -> str | None:
    """Normalize a language hint to a code Sarvam STT accepts (or None)."""
    if not hint:
        return None
    value = str(hint).strip()
    if not value:
        return None
    if "-" in value:
        language, _, region = value.partition("-")
        code = f"{language.lower()}-{region.upper()}"
        return code if code in _STT_LANGUAGE_CODES else None
    return _SARVAM_LANGUAGE_CODES.get(value.lower())


@dataclass
class STTSarvamResult:
    transcript: str
    language_code: str | None
    request_id: str | None
    confidence: float | None = None
    is_mock: bool = False


class STTSarvamClient(abc.ABC):
    @abc.abstractmethod
    async def transcribe(self, audio: bytes, *, mime: str | None, language_hint: str | None) -> STTSarvamResult:
        ...


class SarvamSTT(STTSarvamClient):
    """Saaras STT client (POST /speech-to-text)."""

    name = "sarvam_saaras"
    TIMEOUT = 60.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def transcribe(self, audio: bytes, *, mime: str | None, language_hint: str | None) -> STTSarvamResult:
        settings = self._settings
        if not audio:
            raise STTUnavailableError("The recording is empty.", code="empty_audio")
        url = f"{_base_url(settings)}/speech-to-text"
        # Browsers report e.g. "audio/webm;codecs=opus"; send the bare container
        # type and a matching filename.
        content_type = (mime or "audio/webm").split(";", 1)[0].strip().lower() or "audio/webm"
        filename = f"audio.{_AUDIO_EXTENSIONS.get(content_type, 'webm')}"
        data = {
            "model": settings.sarvam_stt_model,
            # "unknown" is the documented auto-detect value.
            "language_code": sarvam_language_code(language_hint) or "unknown",
        }

        async def _call(headers: dict[str, str]) -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                    response = await client.post(
                        url,
                        headers=headers,
                        files={"file": (filename, audio, content_type)},
                        data=data,
                    )
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    "Sarvam speech-to-text timed out.",
                    provider=self.name, code="provider_timeout", retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Sarvam could not be reached ({type(exc).__name__}).",
                    provider=self.name, code="provider_network_error", retryable=True,
                ) from exc
            error = _sarvam_error(response, self.name, what="audio")
            if error is not None:
                if error.code == "provider_bad_request":
                    error.code = "invalid_audio"
                raise error
            return _sarvam_json(response, self.name)

        try:
            body = await with_retry(
                lambda: _with_key_failover(settings, self.name, _call),
                provider=self.name,
                max_retries=settings.provider_max_retries,
                backoff_base=settings.provider_backoff_base,
                timeout=_attempt_timeout(settings, self.TIMEOUT),
            )
        except ProviderError as exc:
            raise STTUnavailableError(
                exc.message, code=exc.code, details={"provider": self.name, "retryable": exc.retryable}
            ) from exc
        except ProviderUnavailableError as exc:
            raise STTUnavailableError(
                "Sarvam speech-to-text did not respond in time.",
                code="provider_timeout",
                details={"provider": self.name, "retryable": True},
            ) from exc

        transcript = body.get("transcript")
        if not isinstance(transcript, str):
            raise STTUnavailableError(
                "Sarvam returned no transcript field.",
                code="provider_bad_response",
                details={"provider": self.name},
            )
        probability = body.get("language_probability")
        return STTSarvamResult(
            transcript=transcript.strip(),
            language_code=body.get("language_code") if isinstance(body.get("language_code"), str) else None,
            request_id=body.get("request_id") if isinstance(body.get("request_id"), str) else None,
            confidence=float(probability) if isinstance(probability, (int, float)) else None,
        )


class MockSarvamSTT(STTSarvamClient):
    """Deterministic mock transcript with a fake-but-honest request id."""

    name = "mock_stt"

    DEFAULT_TEXT = (
        "Mock transcript: 20% off cold coffee this Saturday and Sunday, "
        "4 PM to 8 PM at our cafe for college students."
    )

    async def transcribe(self, audio: bytes, *, mime: str | None, language_hint: str | None) -> STTSarvamResult:
        digest = hashlib.sha256(b"mock-stt" + audio).hexdigest()[:16]
        return STTSarvamResult(
            transcript=self.DEFAULT_TEXT,
            language_code=language_hint or "en-IN",
            request_id=f"mock-{digest}",
            confidence=0.99,
            is_mock=True,
        )


# ── text-to-speech ────────────────────────────────────────────────────
@dataclass
class TTSResult:
    audio: bytes  # one WAV payload
    request_id: str | None
    provider: str
    model: str
    speaker: str
    is_mock: bool


class TTSClient(abc.ABC):
    name: str = "tts"

    @abc.abstractmethod
    async def synthesize(self, text: str, *, language_code: str, speaker: str) -> TTSResult:
        ...


class SarvamTTS(TTSClient):
    """Bulbul v3 text-to-speech (POST /text-to-speech)."""

    name = "sarvam_bulbul"
    TIMEOUT = 60.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def synthesize(self, text: str, *, language_code: str, speaker: str) -> TTSResult:
        settings = self._settings
        url = f"{_base_url(settings)}/text-to-speech"
        payload = {
            "text": text,
            "language_code": language_code,
            "speaker": speaker,
            "model": TTS_MODEL,
            "output_audio_codec": "wav",
        }

        async def _call(headers: dict[str, str]) -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                    response = await client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    "Sarvam text-to-speech timed out.",
                    provider=self.name, code="provider_timeout", retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Sarvam could not be reached ({type(exc).__name__}).",
                    provider=self.name, code="provider_network_error", retryable=True,
                ) from exc
            error = _sarvam_error(response, self.name, what="speech request")
            if error is not None:
                raise error
            return _sarvam_json(response, self.name)

        try:
            body = await with_retry(
                lambda: _with_key_failover(settings, self.name, _call),
                provider=self.name,
                max_retries=settings.provider_max_retries,
                backoff_base=settings.provider_backoff_base,
                timeout=_attempt_timeout(settings, self.TIMEOUT),
            )
        except ProviderError as exc:
            raise ProviderUnavailableError(
                exc.message, code=exc.code, details=exc.as_dict()
            ) from exc

        audios = body.get("audios")
        if not isinstance(audios, list) or not audios or not isinstance(audios[0], str):
            raise ProviderUnavailableError(
                "Sarvam text-to-speech returned no audio.", code="tts_bad_response"
            )
        try:
            audio = base64.b64decode(audios[0], validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                "Sarvam text-to-speech audio was not valid base64.", code="tts_bad_audio"
            ) from exc
        if not audio:
            raise ProviderUnavailableError(
                "Sarvam text-to-speech audio was empty.", code="tts_bad_audio"
            )
        return TTSResult(
            audio=audio,
            request_id=body.get("request_id") if isinstance(body.get("request_id"), str) else None,
            provider=self.name,
            model=TTS_MODEL,
            speaker=speaker,
            is_mock=False,
        )


class MockTTS(TTSClient):
    """Deterministic offline TTS: a short labelled tone, never real speech."""

    name = "mock_tts"

    async def synthesize(self, text: str, *, language_code: str, speaker: str) -> TTSResult:
        digest = hashlib.sha256(f"{language_code}|{speaker}|{text}".encode("utf-8")).hexdigest()[:16]
        duration = min(6.0, 0.4 + len(text) * 0.02)
        return TTSResult(
            audio=MockVoiceClone._wav(duration),
            request_id=f"mock-tts-{digest}",
            provider=self.name,
            model="mock-tone",
            speaker=speaker,
            is_mock=True,
        )


@dataclass
class VoiceCreateResult:
    voice_id: str
    request_id: str | None
    reference_text: str | None = None


@dataclass
class VoiceCloneResult:
    audio: bytes
    request_id: str | None


class VoiceCloneClient(abc.ABC):
    @abc.abstractmethod
    async def create_voice(self, audio: bytes, *, name: str, language: str, mime: str | None = None) -> VoiceCreateResult:
        ...

    @abc.abstractmethod
    async def clone_speech(self, text: str, *, language_code: str, voice_id: str) -> VoiceCloneResult:
        ...


class SarvamVoiceClone(VoiceCloneClient):
    name = "sarvam_voice"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_voice(self, audio: bytes, *, name: str, language: str, mime: str | None = None) -> VoiceCreateResult:
        settings = self._settings
        url = f"{_base_url(settings)}/voices/create"
        files = {"file": ("reference", audio, mime or "audio/wav")}
        form = {"name": name, "language": language}

        async def _call(headers: dict[str, str]) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, files=files, data=form)
            if response.status_code >= 400:
                raise ProviderError(
                    f"Sarvam voice create failed ({response.status_code}).",
                    provider=self.name,
                    retryable=response.status_code == 429,
                    status_code=response.status_code,
                )
            return response.json()

        body = await with_retry(
            lambda: _with_key_failover(settings, self.name, _call),
            provider=self.name, max_retries=1, backoff_base=1.0,
            timeout=_attempt_timeout(settings, 120.0),
        )
        payload = body.get("data") or {}
        voice_id = payload.get("voice_id") or body.get("voice_id")
        if not voice_id:
            raise ProviderUnavailableError(
                "Sarvam voice create returned no voice_id.",
                code="voice_create_bad_response",
            )
        return VoiceCreateResult(
            voice_id=str(voice_id),
            request_id=payload.get("request_id") or body.get("request_id"),
            reference_text=payload.get("reference_text"),
        )

    async def clone_speech(self, text: str, *, language_code: str, voice_id: str) -> VoiceCloneResult:
        settings = self._settings
        url = f"{_base_url(settings)}/voices/clone"
        data = {"text": text, "language_code": language_code, "voice_id": voice_id}

        async def _call(headers: dict[str, str]) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, data=data)
            if response.status_code >= 400:
                raise ProviderError(
                    f"Sarvam speech clone failed ({response.status_code}).",
                    provider=self.name,
                    retryable=response.status_code in (429,) or response.status_code >= 500,
                    status_code=response.status_code,
                )
            return response.json()

        body = await with_retry(
            lambda: _with_key_failover(settings, self.name, _call),
            provider=self.name, max_retries=1, backoff_base=1.0,
            timeout=_attempt_timeout(settings, 120.0),
        )
        audio_b64 = body.get("audio")
        if not audio_b64:
            raise ProviderUnavailableError(
                "Sarvam clone response contained no audio.",
                code="voice_clone_bad_response",
            )
        try:
            audio = base64.b64decode(audio_b64)
        except Exception as exc:  # noqa: BLE001
            raise ProviderUnavailableError(
                "Sarvam clone audio was not valid base64.", code="voice_clone_bad_audio"
            ) from exc
        if not audio:
            raise ProviderUnavailableError("Sarvam clone audio was empty.", code="voice_clone_empty")
        return VoiceCloneResult(audio=audio, request_id=body.get("request_id"))


class MockVoiceClone(VoiceCloneClient):
    """Deterministic offline voice provider: generates a tiny labelled WAV."""

    name = "mock_voice"

    @staticmethod
    def _wav(seconds: float = 1.0) -> bytes:
        import io
        import struct
        import wave

        rate = 8000
        samples = int(rate * seconds)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            import math

            frames = b"".join(
                struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(samples)
            )
            w.writeframes(frames)
        return buffer.getvalue()

    async def create_voice(self, audio: bytes, *, name: str, language: str, mime: str | None = None) -> VoiceCreateResult:
        digest = hashlib.sha256(audio).hexdigest()[:16]
        return VoiceCreateResult(voice_id=f"mock-voice-{digest}", request_id=f"mock-create-{digest}")

    async def clone_speech(self, text: str, *, language_code: str, voice_id: str) -> VoiceCloneResult:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        duration = min(6.0, 0.15 + len(text) * 0.02)
        return VoiceCloneResult(audio=self._wav(duration), request_id=f"mock-clone-{digest}")


def get_stt_sarvam(settings: Settings | None = None) -> STTSarvamClient:
    """Mock only in mock mode; live mode never degrades to a mock transcript."""
    settings = settings or get_settings()
    if settings.is_mock:
        return MockSarvamSTT()
    if not settings.sarvam_configured:
        raise STTUnavailableError(
            NOT_CONFIGURED_MESSAGE,
            code="provider_not_configured",
            details={"provider": "sarvam_saaras"},
        )
    return SarvamSTT(settings)


def get_tts_client(settings: Settings | None = None) -> TTSClient:
    settings = settings or get_settings()
    if settings.voice_provider == "none":
        raise ProviderUnavailableError(
            "Speech generation is disabled (TITAN_VOICE_PROVIDER=none).",
            code="voice_disabled",
        )
    if settings.is_mock or settings.voice_provider == "mock":
        return MockTTS()
    if not settings.sarvam_configured:
        raise ProviderUnavailableError(NOT_CONFIGURED_MESSAGE, code="provider_not_configured")
    return SarvamTTS(settings)


def get_voice_clone(settings: Settings | None = None, *, profile_is_mock: bool | None = None) -> VoiceCloneClient:
    settings = settings or get_settings()
    if settings.is_mock or profile_is_mock:
        return MockVoiceClone()
    if not settings.sarvam_configured:
        raise ProviderUnavailableError(NOT_CONFIGURED_MESSAGE, code="provider_not_configured")
    return SarvamVoiceClone(settings)


def split_for_clone(text: str, limit: int = MAX_CLONE_TEXT_CHARS) -> list[str]:
    """Deterministic sentence-boundary split honouring the request text limit."""
    if len(text) <= limit:
        return [text]
    import re

    sentences = re.split(r"(?<=[.!?।])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(sentence) > limit:
            # Hard-split very long sentences at word boundaries.
            words = sentence.split(" ")
            for word in words:
                candidate = f"{current} {word}".strip()
                if len(candidate) > limit and current:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
        else:
            candidate = f"{current} {sentence}".strip()
            if len(candidate) > limit and current:
                chunks.append(current)
                current = sentence
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


__all__ = [
    "SUPPORTED_LANGUAGES",
    "LANGUAGE_NAME_BY_CODE",
    "MAX_CLONE_TEXT_CHARS",
    "SARVAM_VERIFY_KEY",
    "SarvamSTT",
    "MockSarvamSTT",
    "SarvamVoiceClone",
    "MockVoiceClone",
    "SarvamTTS",
    "MockTTS",
    "TTSClient",
    "TTSResult",
    "TTS_MODEL",
    "TTS_SPEAKERS",
    "TTS_DEFAULT_SPEAKER",
    "TTS_LANGUAGE_CODES",
    "MAX_TTS_TEXT_CHARS",
    "get_tts_client",
    "get_stt_sarvam",
    "get_voice_clone",
    "split_for_clone",
    "sarvam_live_allowed",
]
