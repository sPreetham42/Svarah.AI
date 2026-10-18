"""Voice profiles + localized cloned-voice generation (Phases 5A/5B, handoff §3).

Consent and ownership are mandatory: a profile may be created only with an
explicit consent flag + recorded consent context, and profiles are owned by the
creating user (``owner_uid``). Reference audio is stored privately; provider
``voice_id`` is persisted and REUSED (never re-created per campaign).

Localized voice assets (5B): the tokenized voice script from the plan is
resolved with the locked token map, synthesized through the provider in
sentence-bounded chunks, concatenated in order, probed (WAV/RIFF header
validation), stored with SHA-256 + full provenance, then round-tripped via ASR
and re-extraction to catch fact mutations. Unsupported-language requests are
refused, never silently degraded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, ProviderUnavailableError, ValidationError
from ..models import AssetRecord, AuditEvent, Campaign, FactSheetRecord, VoiceProfileRecord
from ..services import sarvam
from ..services.async_bridge import run_sync
from ..services.checksums import sha256_hex, sha256_digest, sha256_text
from ..services.fact_engine import substitute_tokens
from ..services.sarvam import SUPPORTED_LANGUAGES


def _audit(db: Session, campaign_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
    db.add(AuditEvent(campaign_id=campaign_id, event_type=event_type, payload_json=json.dumps(payload) if payload else None))


def _voice_owner_uid(payload_owner: str | None) -> str:
    """Resolve the requesting owner (Firebase sub in production).

    NOTE: production deployment passes the verified Firebase UID via the auth
    dependency; in the current pre-auth bytecode (SQLite dev/demo) callers
    supply their development owner id explicitly.
    """
    if not payload_owner or not payload_owner.strip():
        raise ValidationError("owner_uid is required.", code="owner_required")
    return payload_owner.strip()


# ── Phase 5A: profiles ────────────────────────────────────────────────
def create_voice_profile(
    db: Session,
    *,
    owner_uid: str,
    display_name: str,
    consent_confirmed: bool,
    consent_record: dict[str, Any] | None,
    reference_audio: bytes | None,
    reference_mime: str | None,
    reference_transcript: str | None,
    campaign_id: int | None = None,
    shop_id: int | None = None,
) -> VoiceProfileRecord:
    settings = get_settings()
    owner = _voice_owner_uid(owner_uid)
    if not consent_confirmed:
        raise ValidationError(
            "Voice cloning requires explicit consent confirmation.",
            code="consent_required",
        )
    if not display_name or not display_name.strip():
        raise ValidationError("display_name is required.", code="name_required")

    storage_path: str | None = None
    if reference_audio:
        if len(reference_audio) > settings.max_voice_sample_bytes:
            raise ValidationError(
                f"Reference audio exceeds the {settings.max_voice_sample_mb} MB limit.",
                code="audio_too_large",
            )
        from ..storage import get_storage, safe_suffix

        suffix = safe_suffix(reference_mime, fallback=".wav")
        storage_path = f"voices/{owner}/reference_{sha256_hex(reference_audio)}{suffix}"
        get_storage().save_bytes(reference_audio, storage_path)

    provider_client = sarvam.get_voice_clone(settings)
    provider_voice_id: str | None = None
    provider_request_id: str | None = None
    is_mock = provider_client.name.startswith("mock")
    if reference_audio:
        language = consent_record.get("language", "en-IN") if consent_record else "en-IN"
        try:
            result = run_sync(
                provider_client.create_voice(
                    reference_audio, name=display_name.strip(), language=language, mime=reference_mime
                )
            )
        except sarvam.ProviderError as exc:
            raise ProviderUnavailableError(exc.message, code=exc.code) from exc
        provider_voice_id = result.voice_id
        provider_request_id = result.request_id

    record = VoiceProfileRecord(
        campaign_id=campaign_id,
        shop_id=shop_id,
        owner_uid=owner,
        display_name=display_name.strip(),
        consent_confirmed=True,
        consent_record_json=json.dumps(consent_record or {}, ensure_ascii=False),
        reference_audio_path=storage_path,
        reference_transcript=reference_transcript,
        provider=provider_client.name,
        provider_voice_id=provider_voice_id,
        provider_request_id=provider_request_id,
        supported_languages_json=json.dumps(sorted(SUPPORTED_LANGUAGES.values())),
        is_mock=is_mock,
        status="active",
    )
    db.add(record)
    db.flush()
    return record


def get_owned_profile(db: Session, profile_id: int, owner_uid: str) -> VoiceProfileRecord:
    profile = db.get(VoiceProfileRecord, profile_id)
    if profile is None or profile.owner_uid != owner_uid.strip():
        raise NotFoundError(
            f"Voice profile {profile_id} was not found.",
            details={"profile_id": profile_id},
        )
    return profile


def list_profiles(db: Session, owner_uid: str) -> list[VoiceProfileRecord]:
    return (
        db.query(VoiceProfileRecord)
        .filter(VoiceProfileRecord.owner_uid == owner_uid.strip(), VoiceProfileRecord.status == "active")
        .order_by(VoiceProfileRecord.id)
        .all()
    )


def delete_profile(db: Session, profile_id: int, owner_uid: str) -> None:
    profile = get_owned_profile(db, profile_id, owner_uid)
    profile.status = "deleted"
    if profile.reference_audio_path:
        from ..storage import get_storage

        get_storage().delete(profile.reference_audio_path)
    profile.reference_audio_path = None
    db.flush()


def serialize_profile(record: VoiceProfileRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "owner_uid": record.owner_uid,
        "display_name": record.display_name,
        "consent_confirmed": record.consent_confirmed,
        "provider": record.provider,
        "provider_voice_id": record.provider_voice_id,
        "reference_audio_path": record.reference_audio_path,
        "supported_languages": json.loads(record.supported_languages_json or "[]"),
        "is_mock": record.is_mock,
        "status": record.status,
        "created_at": str(record.created_at),
    }


# ── Phase 5B: localized voice assets ──────────────────────────────────
@dataclass
class VoiceSynthesisOutcome:
    audio: bytes
    request_ids: list[str]
    is_mock: bool


def _synthesize_chunks(chunks: list[str], *, language_code: str, voice_id: str, provider) -> VoiceSynthesisOutcome:
    audio_parts: list[bytes] = []
    request_ids: list[str] = []
    for chunk in chunks:
        result = run_sync(provider.clone_speech(chunk, language_code=language_code, voice_id=voice_id))
        audio_parts.append(result.audio)
        request_ids.append(result.request_id or "")
    return VoiceSynthesisOutcome(
        audio=b"".join(audio_parts), request_ids=request_ids, is_mock=provider.name.startswith("mock")
    )


def _validate_wav(audio: bytes) -> float:
    """Validate a RIFF/WAVE payload; returns duration seconds."""
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise ValidationError(
            "Synthesized audio is not a valid WAV payload.",
            code="audio_invalid",
        )
    import wave
    import io

    try:
        with wave.open(io.BytesIO(audio), "rb") as w:
            frames = w.getnframes()
            rate = w.getframerate() or 1
            return frames / float(rate)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("Synthesized audio failed WAV probing.", code="audio_invalid") from exc


def generate_localized_voice(
    db: Session,
    *,
    campaign_id: int,
    profile_id: int,
    owner_uid: str,
    language: str,
) -> AssetRecord:
    """Synthesize the plan's voice_script in ``language`` with the owned profile."""
    settings = get_settings()
    profile = get_owned_profile(db, profile_id, owner_uid)
    if not profile.consent_confirmed:
        raise ConflictError(
            "Voice profile consent was not confirmed; refusing to clone.",
            code="consent_required",
        )
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")

    language_code = SUPPORTED_LANGUAGES.get(language)
    if language_code is None:
        raise ValidationError(
            f"Language {language!r} is not in the supported set "
            f"({', '.join(SUPPORTED_LANGUAGES)}).",
            code="language_unsupported",
        )

    from ..services.campaign_brain import get_plan_record, _plan_from_record

    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}")
    plan = _plan_from_record(plan_record, tokens)

    # Prefer the Language-localized script; fall back to the master voice script.
    script_template = None
    for loc in plan.localization:
        if loc.language == language and "voice_script" in loc.copy_templates:
            script_template = loc.copy_templates["voice_script"]
            break
    if script_template is None:
        master = plan.copy.get("voice_script")
        script_template = master.template if master else None
    if script_template is None:
        raise ValidationError("Plan has no voice_script template.", code="plan_incomplete")

    script = substitute_tokens(script_template, tokens)

    voice_id = profile.provider_voice_id
    if not voice_id:
        raise ConflictError(
            "Voice profile has no provider voice_id; re-create the profile with reference audio.",
            code="voice_id_missing",
        )

    provider = sarvam.get_voice_clone(settings)
    chunks = sarvam.split_for_clone(script)
    try:
        outcome = _synthesize_chunks(chunks, language_code=language_code, voice_id=voice_id, provider=provider)
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise ConflictError(
            f"Voice synthesis failed: {exc}",
            code="voice_synthesis_failed",
        ) from exc

    duration = _validate_wav(outcome.audio)
    from ..storage import get_storage

    relative_path = f"campaigns/{campaign_id}/voices/voice_{profile.id}_{language_code}.wav"
    get_storage().save_bytes(outcome.audio, relative_path)

    record = AssetRecord(
        campaign_id=campaign_id,
        plan_id=plan_record.id,
        factsheet_id=sheet.id,
        fact_hash=sheet.fact_hash,
        kind="voice",
        locale=language_code,
        text_content=script,
        storage_path=relative_path,
        sha256=sha256_digest(outcome.audio),
        asset_status="validating",
        provider=provider.name,
        provider_request_id=",".join(r for r in outcome.request_ids if r) or None,
        is_mock=outcome.is_mock or profile.is_mock,
        used_fallback=provider.name.startswith("mock") and not profile.is_mock,
        provenance_json=json.dumps(
            {
                "voice_profile_id": profile.id,
                "provider_voice_id": voice_id,
                "language": language,
                "language_code": language_code,
                "chunks": len(chunks),
                "duration_seconds": round(duration, 2),
                "script_template_hash": sha256_text(script_template),
                "labeled": "mock_cloned_voice" if outcome.is_mock else "cloned_voice",
            },
            ensure_ascii=False,
        ),
    )
    db.add(record)
    db.flush()
    _audit(
        db,
        campaign_id,
        "asset.voice_generated",
        {"asset_id": record.id, "language": language, "is_mock": record.is_mock},
    )
    return record


# ── Sarvam text-to-speech ─────────────────────────────────────────────
#: Plan channels whose copy can be spoken.
TTS_CHANNELS = ("voice_script", "whatsapp", "instagram", "facebook", "reel_script")


def tts_options() -> dict[str, Any]:
    """What the UI may offer — documented provider values only."""
    return {
        "provider": "sarvam",
        "model": sarvam.TTS_MODEL,
        "languages": list(SUPPORTED_LANGUAGES),
        "speakers": list(sarvam.TTS_SPEAKERS),
        "default_speaker": sarvam.TTS_DEFAULT_SPEAKER,
        "channels": list(TTS_CHANNELS),
        "max_chars": sarvam.MAX_TTS_TEXT_CHARS,
    }


def _concat_wav(parts: list[bytes]) -> bytes:
    """Join WAV payloads frame-wise (a byte join would embed extra headers)."""
    if len(parts) == 1:
        return parts[0]
    import io
    import wave

    out = io.BytesIO()
    params = None
    try:
        with wave.open(out, "wb") as writer:
            for part in parts:
                with wave.open(io.BytesIO(part), "rb") as reader:
                    current = reader.getparams()
                    if params is None:
                        params = current
                        writer.setparams(current)
                    elif current[:3] != params[:3]:
                        raise ValidationError(
                            "Synthesized audio chunks use different formats.",
                            code="audio_invalid",
                        )
                    writer.writeframes(reader.readframes(reader.getnframes()))
    except (wave.Error, EOFError) as exc:
        raise ValidationError(
            "Synthesized audio is not a valid WAV payload.", code="audio_invalid"
        ) from exc
    return out.getvalue()


def generate_tts_voice(
    db: Session,
    *,
    campaign_id: int,
    language: str,
    speaker: str | None = None,
    channel: str = "voice_script",
) -> AssetRecord:
    """Speak one piece of the plan's copy with Sarvam TTS.

    The text is the token-substituted plan copy for ``channel`` — so everything
    spoken is grounded in the locked facts. A provider failure raises; no
    placeholder audio is ever stored in live mode.
    """
    settings = get_settings()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")

    language_code = SUPPORTED_LANGUAGES.get(language)
    if language_code is None or language_code not in sarvam.TTS_LANGUAGE_CODES:
        raise ValidationError(
            f"Language {language!r} is not in the supported set "
            f"({', '.join(SUPPORTED_LANGUAGES)}).",
            code="language_unsupported",
        )
    speaker = (speaker or sarvam.TTS_DEFAULT_SPEAKER).strip().lower()
    if speaker not in sarvam.TTS_SPEAKERS:
        raise ValidationError(
            f"Speaker {speaker!r} is not available for {sarvam.TTS_MODEL}.",
            code="speaker_unsupported",
        )
    if channel not in TTS_CHANNELS:
        raise ValidationError(
            f"Channel {channel!r} cannot be spoken. Choose one of {', '.join(TTS_CHANNELS)}.",
            code="channel_unsupported",
        )

    from ..services.campaign_brain import _plan_from_record, get_plan_record

    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}")
    plan = _plan_from_record(plan_record, tokens)

    existing = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id, AssetRecord.kind == "voice")
        .count()
    )
    if existing >= settings.max_voice_variants:
        raise ConflictError(
            f"Voice budget reached ({settings.max_voice_variants}).", code="budget_exceeded"
        )

    # Prefer the copy localized for this language; fall back to the master.
    script_template = None
    localized = False
    for loc in plan.localization:
        if loc.language == language and channel in loc.copy_templates:
            script_template = loc.copy_templates[channel]
            localized = True
            break
    if script_template is None:
        master = plan.copy.get(channel)
        script_template = master.template if master else None
    if script_template is None:
        raise ValidationError(f"The plan has no {channel} copy.", code="plan_incomplete")
    script = substitute_tokens(script_template, tokens).strip()
    if not script:
        raise ValidationError(f"The plan's {channel} copy is empty.", code="plan_incomplete")

    provider = sarvam.get_tts_client(settings)  # raises when unconfigured/disabled
    chunks = sarvam.split_for_clone(script, sarvam.MAX_TTS_TEXT_CHARS)
    results = [
        run_sync(provider.synthesize(chunk, language_code=language_code, speaker=speaker))
        for chunk in chunks
    ]
    try:
        audio = _concat_wav([r.audio for r in results])
        duration = _validate_wav(audio)
    except ValidationError as exc:
        # The provider answered 200 with unusable audio: that is a provider
        # failure, not a client mistake.
        raise ProviderUnavailableError(
            "Sarvam returned audio that could not be decoded.", code="tts_bad_audio"
        ) from exc
    if duration <= 0:
        raise ProviderUnavailableError("Sarvam returned silent/empty audio.", code="tts_bad_audio")

    from ..storage import get_storage

    is_mock = any(r.is_mock for r in results)
    relative_path = (
        f"campaigns/{campaign_id}/voices/tts_{language_code}_{speaker}_{sha256_hex(audio)[:12]}.wav"
    )
    get_storage().save_bytes(audio, relative_path)

    record = AssetRecord(
        campaign_id=campaign_id,
        plan_id=plan_record.id,
        factsheet_id=sheet.id,
        fact_hash=sheet.fact_hash,
        kind="voice",
        locale=language_code,
        text_content=script,
        template=script_template,
        storage_path=relative_path,
        sha256=sha256_digest(audio),
        asset_status="validating",
        provider=results[0].provider,
        model=results[0].model,
        provider_request_id=",".join(r.request_id for r in results if r.request_id)[:128] or None,
        is_mock=is_mock,
        used_fallback=False,
        provenance_json=json.dumps(
            {
                "engine": "tts",
                "channel": channel,
                "language": language,
                "language_code": language_code,
                "speaker": speaker,
                "localized_copy": localized,
                "chunks": len(chunks),
                "duration_seconds": round(duration, 2),
                "script_template_hash": sha256_text(script_template),
                "labeled": "mock_tts" if is_mock else "tts",
            },
            ensure_ascii=False,
        ),
    )
    db.add(record)
    db.flush()
    _audit(
        db,
        campaign_id,
        "asset.voice_generated",
        {"asset_id": record.id, "language": language, "engine": "tts", "is_mock": is_mock},
    )
    return record
