"""Campaign endpoints (Source of Truth §46).

Phase 2 scope: create (typed text or audio) + retrieve + list, and separate
transcription from fact extraction (`POST /campaigns/{id}/extract`). Locking
and generation are deliberately deferred to later phases.
"""

from __future__ import annotations

import base64
import binascii
import json

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import (
    ConflictError,
    ExtractionUnavailableError,
    NotFoundError,
    ProviderUnavailableError,
    STTUnavailableError,
    ValidationError,
)
from ..models import (
    AuditEvent,
    Campaign,
    CampaignStatus,
    FactSheetStatus,
    InputType,
    Shop,
    User,
)
from ..schemas import (
    AuditEventView,
    CampaignCreate,
    CampaignRead,
    CampaignSummary,
    ExtractionStatus,
    STTStatus,
    TranscriptView,
    TypedTranscriptIn,
)
from ..services.extraction import get_extraction_provider
from ..services.fact_engine import normalize_fact_data
from ..services.fact_validation import refresh_deterministic, validate_sheet
from ..services.factsheet_service import (
    latest_sheet,
    list_versions,
    record_extraction,
    serialize_sheet,
    version_summary,
)
from ..services.stt import get_stt_provider, normalize_transcript
from ..storage import get_storage, safe_suffix

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

DEFAULT_SHOP_NAME = "Demo Shop"


# ── helpers ────────────────────────────────────────────────────────────
def _ensure_default_shop(db: Session, user: User) -> Shop:
    """The caller's own default shop, created on first use.

    Scoped by owner: two accounts never share the shop row that their
    campaigns hang off, even though both are called "Demo Shop".
    """
    shop = db.scalar(
        select(Shop).where(Shop.owner_uid == user.owner_uid).order_by(Shop.id).limit(1)
    )
    if shop is None:
        shop = Shop(
            name=user.display_name or DEFAULT_SHOP_NAME,
            locale="en",
            owner_uid=user.owner_uid,
        )
        db.add(shop)
        db.flush()
    return shop


def _resolve_shop(db: Session, shop_id: int | None, user: User) -> Shop:
    if shop_id is None:
        return _ensure_default_shop(db, user)
    shop = db.get(Shop, shop_id)
    # A shop belonging to another account is reported as missing, not
    # forbidden, so ids cannot be probed for existence.
    if shop is None or shop.owner_uid != user.owner_uid:
        raise NotFoundError(f"Shop {shop_id} was not found.", details={"shop_id": shop_id})
    return shop


def _record_audit(db: Session, campaign: Campaign, event_type: str, payload: dict | None = None) -> None:
    db.add(
        AuditEvent(
            campaign_id=campaign.id,
            event_type=event_type,
            payload_json=json.dumps(payload) if payload else None,
        )
    )


def _decode_audio(audio_b64: str, max_bytes: int) -> bytes:
    try:
        data = base64.b64decode(audio_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValidationError("'audio_b64' is not valid base64.", code="invalid_audio") from exc
    if not data:
        raise ValidationError("Decoded audio is empty.", code="empty_audio")
    if len(data) > max_bytes:
        raise ValidationError(
            f"Audio exceeds the {max_bytes} byte limit.", code="audio_too_large"
        )
    return data


def _transcript_view(campaign: Campaign) -> TranscriptView | None:
    if campaign.transcript is None:
        return None
    meta = json.loads(campaign.transcript_meta_json) if campaign.transcript_meta_json else {}
    return TranscriptView(
        raw=campaign.transcript,
        normalized=campaign.normalized_transcript,
        hash=campaign.transcript_hash,
        language=meta.get("language"),
        duration_seconds=meta.get("duration_seconds"),
        confidence=meta.get("confidence"),
        provider=meta.get("provider"),
        is_mock=bool(meta.get("is_mock")),
        segments=meta.get("segments") or [],
    )


def _serialize(
    db: Session,
    campaign: Campaign,
    *,
    stt: STTStatus | None = None,
    include_events: bool = True,
) -> CampaignRead:
    events: list[AuditEventView] = []
    if include_events:
        rows = db.scalars(
            select(AuditEvent)
            .where(AuditEvent.campaign_id == campaign.id)
            .order_by(AuditEvent.id)
        ).all()
        events = [
            AuditEventView(
                id=e.id,
                event_type=e.event_type,
                payload=json.loads(e.payload_json) if e.payload_json else None,
                created_at=e.created_at,
            )
            for e in rows
        ]

    sheet = latest_sheet(db, campaign.id)
    factsheet = serialize_sheet(sheet) if sheet is not None else None
    versions = [version_summary(s) for s in list_versions(db, campaign.id)]

    return CampaignRead(
        id=campaign.id,
        shop_id=campaign.shop_id,
        status=campaign.status,
        input_type=campaign.input_type,
        transcript=_transcript_view(campaign),
        audio_path=campaign.audio_path,
        facts=factsheet.facts if factsheet is not None else None,
        factsheet=factsheet,
        factsheet_versions=versions,
        assets=[],
        verification=[],
        audit_events=events,
        stt=stt,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
    )


# ── extraction ─────────────────────────────────────────────────────────
async def _run_extraction(
    db: Session,
    campaign: Campaign,
    settings: Settings,
    *,
    business_name: str | None,
) -> ExtractionStatus:
    """Extract drafted facts from the campaign transcript.

    Never fabricates: if the provider is unavailable the campaign keeps an
    empty (truthful) draft and the client is told to enter facts manually.
    """
    fallback = normalize_fact_data({})
    transcript = campaign.transcript or ""

    try:
        provider = get_extraction_provider(settings)
        result = await provider.extract(transcript, business_name=business_name)
    except (ExtractionUnavailableError, ProviderUnavailableError) as exc:
        status_view = ExtractionStatus(
            status="unavailable",
            provider=getattr(exc, "provider", None) or settings.extraction_provider,
            is_mock=False,
            message=getattr(exc, "message", str(exc)),
            fallback="manual_entry",
        )
        sheet = record_extraction(
            db, campaign, extracted=None, extraction=status_view.model_dump(), fallback_facts=fallback
        )
        refresh_deterministic(sheet)
        db.commit()
        return status_view
    except Exception as exc:  # noqa: BLE001 - unexpected provider failure
        status_view = ExtractionStatus(
            status="error",
            provider=settings.extraction_provider,
            is_mock=False,
            message="Fact extraction failed. Enter the facts manually.",
            fallback="manual_entry",
        )
        sheet = record_extraction(
            db,
            campaign,
            extracted=None,
            extraction=status_view.model_dump(),
            fallback_facts=fallback,
        )
        refresh_deterministic(sheet)
        _record_audit(db, campaign, "facts.extraction_error", {"error": type(exc).__name__})
        db.commit()
        return status_view

    status_view = ExtractionStatus(
        status="ok",
        provider=result.provider,
        is_mock=result.is_mock,
        message=result.message,
    )
    sheet = record_extraction(
        db,
        campaign,
        extracted=result.sheet,
        extraction=status_view.model_dump(),
        fallback_facts=fallback,
    )
    # Extraction proposes; validation checks. Deterministic rules always run,
    # then Agnes compares the facts with the transcript. A failed semantic
    # call is stored as "unavailable" and never blocks the owner's review.
    validation = await validate_sheet(sheet, transcript, settings)
    _record_audit(
        db,
        campaign,
        "facts.validated",
        {
            "status": validation["status"],
            "semantic": (validation.get("semantic") or {}).get("status"),
        },
    )
    db.commit()
    return status_view


# ── routes ─────────────────────────────────────────────────────────────
@router.post("", status_code=status.HTTP_201_CREATED, response_model=CampaignRead)
async def create_campaign(
    payload: CampaignCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CampaignRead:
    settings = get_settings()
    shop = _resolve_shop(db, payload.shop_id, user)

    campaign = Campaign(
        shop_id=shop.id, owner_uid=user.owner_uid, status=CampaignStatus.CAPTURED
    )
    db.add(campaign)
    db.flush()
    _record_audit(db, campaign, "campaign.created", {"shop_id": shop.id})

    # Typed-text path (also the universal fallback).
    if payload.text:
        normalized = normalize_transcript(payload.text)
        campaign.input_type = InputType.TYPED
        campaign.transcript = payload.text
        campaign.normalized_transcript = normalized
        campaign.transcript_hash = _hash_text(payload.text)
        campaign.transcript_meta_json = json.dumps(
            {
                "provider": "typed",
                "is_mock": False,
                "language": payload.language_hint,
                "duration_seconds": None,
                "confidence": None,
                "segments": [],
            }
        )
        _record_audit(db, campaign, "transcript.captured", {"source": "typed"})
        db.commit()
        await _run_extraction(db, campaign, settings, business_name=shop.name)
        return _serialize(db, campaign)

    # Audio path: store raw audio, attempt STT, degrade to typed fallback.
    audio = _decode_audio(payload.audio_b64 or "", settings.max_audio_bytes)
    suffix = safe_suffix(payload.audio_mime)
    relative_path = f"campaigns/{campaign.id}/source/audio{suffix}"
    get_storage().save_bytes(audio, relative_path)
    campaign.input_type = InputType.AUDIO
    campaign.audio_path = relative_path
    _record_audit(db, campaign, "audio.stored", {"path": relative_path, "bytes": len(audio)})

    stt_status_view: STTStatus
    try:
        provider = get_stt_provider(settings)
        result = await provider.transcribe(
            audio, mime=payload.audio_mime, language_hint=payload.language_hint
        )
    except (STTUnavailableError, ProviderUnavailableError) as exc:
        # Graceful degradation: keep the campaign + audio, prompt typed input.
        stt_status_view = STTStatus(
            status="unavailable",
            provider=getattr(exc, "provider", None) or settings.stt_provider,
            code=getattr(exc, "code", "stt_unavailable"),
            message=getattr(exc, "message", str(exc)),
            fallback="typed_text",
        )
        _record_audit(db, campaign, "stt.unavailable", {"message": stt_status_view.message})
    except Exception as exc:  # noqa: BLE001 - unexpected provider failure
        stt_status_view = STTStatus(
            status="error",
            provider=settings.stt_provider,
            code="stt_error",
            message="Speech-to-text failed. Please type your message instead.",
            fallback="typed_text",
        )
        _record_audit(db, campaign, "stt.error", {"error": str(exc)})
    else:
        campaign.transcript = result.raw_transcript
        campaign.normalized_transcript = result.normalized_transcript
        campaign.transcript_hash = result.transcript_hash
        campaign.transcript_meta_json = json.dumps(result.metadata())
        stt_status_view = STTStatus(
            status="ok",
            provider=result.provider,
            code=None,
            message="Transcribed (mock provider)." if result.is_mock else "Transcribed.",
        )
        _record_audit(
            db,
            campaign,
            "transcript.captured",
            {"source": "audio", "provider": result.provider, "is_mock": result.is_mock},
        )

    db.commit()
    if campaign.transcript:
        await _run_extraction(db, campaign, settings, business_name=shop.name)
    else:
        # STT failed or is disabled: keep the campaign + audio and guarantee a
        # draft FactSheet the client can fill by hand (Source of Truth §46:
        # typed input is the universal fallback).
        _ensure_factsheet_for_manual_entry(
            db,
            campaign,
            message=stt_status_view.message or "Enter the facts manually below.",
        )
    return _serialize(db, campaign, stt=stt_status_view)


@router.get("", response_model=list[CampaignSummary])
def list_campaigns(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[CampaignSummary]:
    rows = db.scalars(
        select(Campaign)
        .where(Campaign.owner_uid == user.owner_uid)
        .order_by(Campaign.id.desc())
        .limit(limit)
    ).all()
    return [
        CampaignSummary(
            id=c.id,
            shop_id=c.shop_id,
            status=c.status,
            input_type=c.input_type,
            has_transcript=c.transcript is not None,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in rows
    ]


@router.get("/{campaign_id}", response_model=CampaignRead)
def get_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CampaignRead:
    campaign = owned_campaign(db, campaign_id, user)
    return _serialize(db, campaign)


@router.post("/{campaign_id}/transcript", response_model=CampaignRead)
async def set_campaign_transcript(
    campaign_id: int,
    payload: TypedTranscriptIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CampaignRead:
    """Attach a typed transcript to a campaign whose STT failed.

    This is the recovery path for the STT-unavailable state: the recorded
    audio is kept, the typed brief becomes the immutable transcript, and fact
    extraction runs on it immediately.
    """
    campaign = owned_campaign(db, campaign_id, user)
    if campaign.transcript:
        raise ConflictError(
            "This campaign already has a transcript.",
            code="transcript_exists",
        )
    sheet = latest_sheet(db, campaign.id)
    if sheet is not None and sheet.status == FactSheetStatus.LOCKED:
        raise ConflictError(
            "Facts are already locked.",
            code="facts_locked",
            details={"factsheet_id": sheet.id},
        )
    text = payload.text.strip()
    if not text:
        raise ValidationError("The transcript text is empty.", code="empty_text")

    settings = get_settings()
    normalized = normalize_transcript(text)
    campaign.input_type = InputType.TYPED
    campaign.transcript = text
    campaign.normalized_transcript = normalized
    campaign.transcript_hash = _hash_text(text)
    campaign.transcript_meta_json = json.dumps(
        {
            "provider": "typed",
            "is_mock": False,
            "language": payload.language_hint,
            "duration_seconds": None,
            "confidence": None,
            "segments": [],
        }
    )
    _record_audit(db, campaign, "transcript.captured", {"source": "typed_recovery"})
    db.commit()
    shop = db.get(Shop, campaign.shop_id)
    await _run_extraction(db, campaign, settings, business_name=shop.name if shop else None)
    return _serialize(db, campaign, stt=STTStatus(status="skipped", provider="typed"))


@router.post("/{campaign_id}/extract", response_model=CampaignRead)
async def extract_campaign_facts(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> CampaignRead:
    """Re-run fact extraction on the stored transcript (transcription stays separate)."""
    campaign = owned_campaign(db, campaign_id, user)
    if not campaign.transcript:
        raise ValidationError(
            "Cannot extract facts: the campaign has no transcript. "
            "Submit typed text or record audio first.",
            code="missing_transcript",
        )
    settings = get_settings()
    sheet = latest_sheet(db, campaign.id)
    if sheet is not None and sheet.status == FactSheetStatus.LOCKED:
        raise ConflictError(
            "Facts are already locked. Edit the locked version to create a new "
            "draft before extracting again.",
            code="facts_locked",
            details={"factsheet_id": sheet.id},
        )
    shop = db.get(Shop, campaign.shop_id)
    await _run_extraction(
        db, campaign, settings, business_name=shop.name if shop else None
    )
    return _serialize(db, campaign)


def _hash_text(text: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _ensure_factsheet_for_manual_entry(
    db: Session,
    campaign: Campaign,
    *,
    message: str,
) -> None:
    """Guarantee a draft FactSheet exists even when extraction was skipped.

    When STT fails there is no transcript, so `_run_extraction` has nothing to
    work with. The client's facts view needs a (truthfully empty) draft sheet
    to render the manual-entry form — without one the UI dead-ends on a banner
    whose only escape ("re-run extraction") cannot succeed either.
    """
    sheet = latest_sheet(db, campaign.id)
    if sheet is not None:
        return
    fallback = normalize_fact_data({})
    status_view = ExtractionStatus(
        status="unavailable",
        provider=None,
        is_mock=False,
        message=message,
        fallback="manual_entry",
    )
    sheet = record_extraction(
        db,
        campaign,
        extracted=None,
        extraction=status_view.model_dump(),
        fallback_facts=fallback,
    )
    refresh_deterministic(sheet)
    db.commit()
