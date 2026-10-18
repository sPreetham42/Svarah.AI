"""Voice endpoints (Phases 5A/5B).

The owner of a voice profile is the authenticated account. `owner_uid` is no
longer accepted from the client on any of these routes — a cloned voice is
exactly the kind of resource a guessable identifier must never unlock.
"""

from __future__ import annotations

import base64
import binascii

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import ValidationError
from ..models import User
from ..services.assets_service import serialize_asset
from ..services.voice_service import (
    create_voice_profile,
    delete_profile,
    generate_localized_voice,
    generate_tts_voice,
    get_owned_profile,
    list_profiles,
    serialize_profile,
    tts_options,
)

router = APIRouter(tags=["voice"])

SUPPORTED = ["English", "Hindi", "Kannada", "Tamil", "Telugu"]


class ProfileCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=128)
    consent_confirmed: bool
    consent_record: dict | None = None
    reference_audio_b64: str | None = None
    reference_audio_mime: str | None = Field(default=None, max_length=128)
    reference_transcript: str | None = None
    campaign_id: int | None = None
    shop_id: int | None = None


class VoiceGenerateRequest(BaseModel):
    profile_id: int
    language: str


@router.post("/voice-profiles", response_model=dict, status_code=201)
def create_profile(
    payload: ProfileCreate,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    audio: bytes | None = None
    if payload.reference_audio_b64:
        try:
            audio = base64.b64decode(payload.reference_audio_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationError("'reference_audio_b64' is not valid base64.", code="invalid_audio") from exc
    # A campaign may only be attached to a profile by its own owner.
    if payload.campaign_id is not None:
        owned_campaign(db, payload.campaign_id, user)
    record = create_voice_profile(
        db,
        owner_uid=user.owner_uid,
        display_name=payload.display_name,
        consent_confirmed=payload.consent_confirmed,
        consent_record=payload.consent_record,
        reference_audio=audio,
        reference_mime=payload.reference_audio_mime,
        reference_transcript=payload.reference_transcript,
        campaign_id=payload.campaign_id,
        shop_id=payload.shop_id,
    )
    db.commit()
    return serialize_profile(record)


@router.get("/voice-profiles", response_model=list[dict])
def get_profiles(
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    return [serialize_profile(p) for p in list_profiles(db, user.owner_uid)]


@router.get("/voice-profiles/{profile_id}", response_model=dict)
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return serialize_profile(get_owned_profile(db, profile_id, user.owner_uid))


@router.delete("/voice-profiles/{profile_id}", status_code=204)
def remove_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    delete_profile(db, profile_id, user.owner_uid)
    db.commit()
    return Response(status_code=204)


@router.post("/campaigns/{campaign_id}/voice", response_model=dict, status_code=201)
def generate_voice(
    campaign_id: int,
    payload: VoiceGenerateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    record = generate_localized_voice(
        db,
        campaign_id=campaign_id,
        profile_id=payload.profile_id,
        owner_uid=user.owner_uid,
        language=payload.language,
    )
    db.commit()
    return serialize_asset(record)


class TTSRequest(BaseModel):
    language: str = Field(min_length=1, max_length=32)
    speaker: str | None = Field(default=None, max_length=64)
    #: Which piece of plan copy to speak (default: the voice script).
    channel: str = Field(default="voice_script", max_length=32)


@router.get("/voice/options", response_model=dict)
def voice_options(user: User = Depends(current_user)) -> dict:
    """Languages, speakers and channels the TTS endpoint accepts."""
    return tts_options()


@router.post("/campaigns/{campaign_id}/tts", response_model=dict, status_code=201)
def generate_tts(
    campaign_id: int,
    payload: TTSRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Speak plan copy with Sarvam TTS and store it as a voice asset.

    Sync on purpose: FastAPI runs it in the threadpool, so the provider call
    never blocks the event loop. Errors come back with the provider's reason.
    """
    owned_campaign(db, campaign_id, user)
    record = generate_tts_voice(
        db,
        campaign_id=campaign_id,
        language=payload.language,
        speaker=payload.speaker,
        channel=payload.channel,
    )
    db.commit()
    return serialize_asset(record)
