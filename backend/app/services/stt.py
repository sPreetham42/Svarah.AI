"""Speech-to-text provider abstraction (Source of Truth §7).

Sarvam Saaras is the only speech-to-text provider:

    TITAN_MODE=mock  -> deterministic, labelled mock transcript
    TITAN_MODE=live  -> Sarvam Saaras (real API call)
        -> on failure: typed input (handled by the API layer, with the
           provider's reason shown to the user)

There is no silent switch to another engine. The legacy `faster-whisper`
provider class remains only for an operator who selects it explicitly; `auto`
never picks it.

The raw transcript is immutable. Normalization here is deliberately
non-destructive (Unicode NFC + whitespace collapse) — semantic/fact
normalization belongs to the Guardian in a later phase.
"""

from __future__ import annotations

import abc
import hashlib
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError, STTUnavailableError
from .gateway import ProviderStatus

try:  # anyio ships with FastAPI/Starlette; run sync model calls off the loop.
    from anyio import to_thread
except Exception:  # pragma: no cover - anyio is always present with FastAPI
    to_thread = None  # type: ignore[assignment]


def normalize_transcript(text: str) -> str:
    """Non-destructive transcript normalization."""
    text = unicodedata.normalize("NFC", text)
    return " ".join(text.split()).strip()


@dataclass
class TranscriptSegment:
    start: float | None
    end: float | None
    text: str
    confidence: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "confidence": self.confidence,
        }


@dataclass
class TranscriptResult:
    raw_transcript: str
    provider: str
    is_mock: bool = False
    language: str | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    segments: list[TranscriptSegment] = field(default_factory=list)

    @property
    def normalized_transcript(self) -> str:
        return normalize_transcript(self.raw_transcript)

    @property
    def transcript_hash(self) -> str:
        digest = hashlib.sha256(self.raw_transcript.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def metadata(self) -> dict[str, Any]:
        """Transcript metadata persisted alongside the immutable raw text."""
        return {
            "provider": self.provider,
            "is_mock": self.is_mock,
            "language": self.language,
            "duration_seconds": self.duration_seconds,
            "confidence": self.confidence,
            "segments": [s.as_dict() for s in self.segments],
            "transcript_hash": self.transcript_hash,
        }


class STTProvider(abc.ABC):
    name: str = "stt"

    @abc.abstractmethod
    async def transcribe(
        self, audio: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> TranscriptResult:
        ...


class MockSTTProvider(STTProvider):
    """Deterministic, clearly-labelled transcript for offline/mock mode.

    This does not impersonate a live provider: `is_mock=True` and the
    provider name is `mock`, so downstream code and the UI can label it.
    """

    name = "mock"

    DEFAULT_TEXT = (
        "Mock transcript: 20% off cold coffee this Saturday and Sunday, "
        "4 PM to 8 PM at our cafe for college students."
    )

    async def transcribe(
        self, audio: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> TranscriptResult:
        text = self.DEFAULT_TEXT
        return TranscriptResult(
            raw_transcript=text,
            provider=self.name,
            is_mock=True,
            language=language_hint or "en",
            duration_seconds=None,
            confidence=None,
            segments=[
                TranscriptSegment(start=0.0, end=None, text=text, confidence=None)
            ],
        )


class FasterWhisperSTTProvider(STTProvider):
    """Lazily-initialized faster-whisper local fallback.

    The model is only loaded on the first transcription request. If
    faster-whisper is not installed (or the model cannot load), a normalized
    `STTUnavailableError` is raised so the API can fall back to typed input.
    """

    name = "faster-whisper"

    def __init__(self, model_size: str, compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.compute_type = compute_type
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel  # type: ignore import-not-found
        except Exception as exc:  # ImportError or binary load failure
            raise STTUnavailableError(
                "faster-whisper is not installed. Install the optional "
                "requirements or use typed input.",
                details={"reason": "not_installed", "error": str(exc)},
            ) from exc
        try:
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type=self.compute_type
            )
        except Exception as exc:  # pragma: no cover - depends on local runtime
            raise STTUnavailableError(
                "Failed to load the faster-whisper model.",
                details={"reason": "model_load_failed", "error": str(exc)},
            ) from exc
        return self._model

    def _transcribe_sync(
        self, audio: bytes, language_hint: str | None
    ) -> TranscriptResult:
        import io

        model = self._load_model()
        segments_iter, info = model.transcribe(
            io.BytesIO(audio), language=language_hint, beam_size=1
        )
        segments: list[TranscriptSegment] = []
        parts: list[str] = []
        for seg in segments_iter:
            parts.append(seg.text)
            segments.append(
                TranscriptSegment(
                    start=float(seg.start) if seg.start is not None else None,
                    end=float(seg.end) if seg.end is not None else None,
                    text=seg.text.strip(),
                    confidence=None,
                )
            )
        raw = "".join(parts).strip()
        duration = getattr(info, "duration", None)
        return TranscriptResult(
            raw_transcript=raw,
            provider=self.name,
            is_mock=False,
            language=getattr(info, "language", None) or language_hint,
            duration_seconds=float(duration) if duration is not None else None,
            confidence=None,
            segments=segments,
        )

    async def transcribe(
        self, audio: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> TranscriptResult:
        if not audio:
            raise STTUnavailableError("Empty audio payload.")
        if to_thread is None:  # pragma: no cover
            return self._transcribe_sync(audio, language_hint)
        try:
            return await to_thread.run_sync(self._transcribe_sync, audio, language_hint)
        except STTUnavailableError:
            raise
        except Exception as exc:
            raise STTUnavailableError(
                "Speech-to-text failed.",
                details={"reason": "transcription_error", "error": str(exc)},
            ) from exc


class SarvamSaarasSTTProvider(STTProvider):
    """Sarvam Saaras v4 primary (Phase 5A/5B) with provenance metadata."""

    name = "sarvam_saaras"

    async def transcribe(
        self, audio: bytes, *, mime: str | None = None, language_hint: str | None = None
    ) -> TranscriptResult:
        from .sarvam import get_stt_sarvam

        result = await get_stt_sarvam().transcribe(
            audio, mime=mime, language_hint=language_hint
        )
        if not result.transcript:
            raise STTUnavailableError(
                "No speech was recognised in the recording. Try again closer to the "
                "microphone, or type the brief.",
                code="empty_transcript",
                details={"reason": "empty_transcript"},
            )
        return TranscriptResult(
            raw_transcript=result.transcript,
            provider="sarvam_saaras_mock" if result.is_mock else "sarvam_saaras",
            is_mock=result.is_mock,
            language=result.language_code or language_hint,
            duration_seconds=None,
            confidence=result.confidence,
            segments=[TranscriptSegment(start=None, end=None, text=result.transcript, confidence=result.confidence)],
        )


def get_stt_provider(settings: Settings | None = None) -> STTProvider:
    """Resolve the active STT provider from settings.

    `auto`: mock mode → mock; live mode → Sarvam Saaras. Typed input remains
    the API-layer fallback when the provider fails.
    """
    settings = settings or get_settings()
    choice = settings.stt_provider

    if choice == "none":
        raise STTUnavailableError(
            "Speech-to-text is disabled (TITAN_STT_PROVIDER=none). Use typed input.",
            details={"reason": "disabled"},
        )
    if choice == "mock":
        return MockSTTProvider()
    if choice == "sarvam":
        return SarvamSaarasSTTProvider()
    if choice == "faster-whisper":
        return FasterWhisperSTTProvider(
            settings.whisper_model, settings.whisper_compute_type
        )
    # auto: mock mode → mock; live mode → Sarvam (which reports a missing key
    # as a configuration error instead of switching engines).
    if settings.is_mock:
        return MockSTTProvider()
    return SarvamSaarasSTTProvider()


def stt_status(settings: Settings | None = None) -> ProviderStatus:
    """Provider-status view for the STT layer (used by GET /api/modes)."""
    settings = settings or get_settings()
    choice = settings.stt_provider
    mode = settings.titan_mode

    if choice == "none":
        return ProviderStatus(
            name="stt",
            kind="stt",
            mode=mode,
            configured=False,
            available=False,
            verified=True,
            detail="Disabled by configuration; typed input is the fallback.",
            capabilities=["typed_fallback"],
        )
    if choice == "mock" or (choice == "auto" and settings.is_mock):
        return ProviderStatus(
            name="stt",
            kind="stt",
            mode=mode,
            configured=True,
            available=True,
            verified=True,
            detail="Mock STT provider active (offline-safe, labelled is_mock).",
            capabilities=["mock_transcript"],
        )

    if choice in ("sarvam", "auto"):
        if settings.is_mock:
            return ProviderStatus(
                name="stt",
                kind="stt",
                mode=mode,
                configured=True,
                available=True,
                verified=True,
                detail="Sarvam selected, but TITAN_MODE=mock: labelled mock transcripts are returned.",
                capabilities=["mock_transcript"],
            )
        configured = settings.sarvam_configured
        return ProviderStatus(
            name="stt",
            kind="stt",
            mode=mode,
            configured=configured,
            available=configured,
            verified=configured,
            detail=(
                f"Sarvam {settings.sarvam_stt_model} speech-to-text active."
                if configured
                else "Sarvam speech-to-text is not configured: set TITAN_SARVAM_API_KEY."
            ),
            capabilities=["sarvam_stt", "typed_fallback"],
        )

    installed = _faster_whisper_installed()
    return ProviderStatus(
        name="stt",
        kind="stt",
        mode=mode,
        configured=True,
        available=installed,
        verified=installed,
        detail=(
            f"faster-whisper '{settings.whisper_model}' (explicitly selected) "
            + ("is installed (lazy-loaded on first use)." if installed else "is NOT installed; typed fallback will be used.")
        ),
        capabilities=["faster_whisper", "typed_fallback"],
    )


def _faster_whisper_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("faster_whisper") is not None


__all__ = [
    "STTProvider",
    "MockSTTProvider",
    "FasterWhisperSTTProvider",
    "TranscriptResult",
    "TranscriptSegment",
    "get_stt_provider",
    "stt_status",
    "normalize_transcript",
    "ProviderUnavailableError",
]
