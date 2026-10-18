"""ASR round-trip check (Phase 7): transcribe the rendered audio, compare facts.

Uses the active STT provider stack (Source of Truth §7) through the shared
:func:`run_sync` bridge:

- a live transcript is checked with the deterministic text checks against the
  locked token map — a mutated value (e.g. "20%" heard as "25%") is a critical
  FAIL;
- a mock transcript is a deterministic fixture that does NOT reflect the actual
  audio, so it is reported NEEDS_REVIEW with the provider disclosed — never a
  silent pass;
- a disabled/unavailable/erroring STT stack is reported NEEDS_REVIEW with the
  reason, so the certificate states honestly that the audio was not confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import STTUnavailableError
from ..guardian.checks_text import CheckOutcome, verify_text
from .async_bridge import run_sync
from .checksums import sha256_text


@dataclass
class _Transcription:
    """Outcome of one ASR attempt (never raises: failures are reported)."""

    text: str | None
    provider: str | None
    is_mock: bool
    unavailable_reason: str | None = None
    error: str | None = None


def _transcribe(audio: bytes, language_code: str | None) -> _Transcription:
    from ..config import get_settings
    from .stt import get_stt_provider

    settings = get_settings()
    try:
        provider = get_stt_provider(settings)
    except STTUnavailableError as exc:
        return _Transcription(
            text=None,
            provider=None,
            is_mock=False,
            unavailable_reason=exc.message,
        )
    except Exception as exc:  # noqa: BLE001 - provider resolution must not break verification
        return _Transcription(text=None, provider=None, is_mock=False, error=str(exc))
    try:
        result = run_sync(provider.transcribe(audio, language_hint=language_code))
    except STTUnavailableError as exc:
        return _Transcription(
            text=None,
            provider=provider.name,
            is_mock=False,
            unavailable_reason=exc.message,
        )
    except Exception as exc:  # noqa: BLE001 - a provider crash is an honest NEEDS_REVIEW
        return _Transcription(text=None, provider=provider.name, is_mock=False, error=str(exc))
    return _Transcription(
        text=result.raw_transcript,
        provider=result.provider,
        is_mock=bool(result.is_mock),
    )


def roundtrip_check(
    audio: bytes,
    script: str,
    tokens: dict[str, str],
    *,
    language_code: str | None = None,
) -> CheckOutcome:
    """Compare what was *heard* against the locked facts; never silently pass."""
    attempt = _transcribe(audio, language_code)
    evidence: dict[str, Any] = {
        "language_code": language_code,
        "script_hash": sha256_text(script),
        "provider": attempt.provider,
        "audio_bytes": len(audio),
    }

    if attempt.text is None:
        note = "ASR unavailable for round-trip; cannot confirm the spoken audio."
        if attempt.unavailable_reason:
            note = f"ASR unavailable for round-trip ({attempt.unavailable_reason})."
        elif attempt.error:
            note = f"ASR failed during round-trip ({attempt.error})."
        return CheckOutcome(
            "asr_roundtrip",
            "NEEDS_REVIEW",
            0.0,
            {**evidence, "note": note, "script_excerpt": script[:200]},
        )

    outcomes = verify_text(attempt.text, tokens)
    failures = [o.check for o in outcomes if o.verdict == "FAIL"]
    evidence["transcript_excerpt"] = attempt.text[:300]

    if attempt.is_mock:
        # The mock provider returns a fixed fixture, so a PASS here would be a
        # fabricated verification. Disclose the limitation instead.
        return CheckOutcome(
            "asr_roundtrip",
            "NEEDS_REVIEW",
            0.3,
            {
                **evidence,
                "note": (
                    "ASR ran with the mock STT provider; the transcript is a deterministic "
                    "fixture and does not reflect the rendered audio."
                ),
                "deterministic_failures": failures,
            },
        )
    if failures:
        return CheckOutcome(
            "asr_roundtrip",
            "FAIL",
            0.9,
            {**evidence, "failed_checks": failures},
        )
    return CheckOutcome("asr_roundtrip", "PASS", 0.8, evidence)


__all__ = ["roundtrip_check"]
