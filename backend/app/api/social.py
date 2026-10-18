"""Posting a finished campaign to the owner's own social accounts.

The voice workspace asks, once the package is ready, "shall I post this for
you?". The spoken answer is read by ``publish-turn`` (it never posts by
itself); ``social/post`` is the outward-facing action and repeats the
verified-assets gate on the server.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import ProviderUnavailableError, STTUnavailableError, TitanError
from ..models import User
from ..services import social_service
from ..services.agnes import get_llm_client, parse_llm_json
from ..services.stt import get_stt_provider
from .campaigns import _decode_audio, _record_audit
from .voice_turn import VoiceTurnIn, quick_intent

router = APIRouter(tags=["social"])

#: A reply made only of these words is a plain "no" (no model call needed).
_NEGATIVE = frozenset(
    """no nope nah not now later dont do never skip thanks thank you please its it is ok okay fine
    that all for nothing else leave i will myself post
    nahi nahin nahee mat abhi baad mein beda illa vendam vendaam ledu vaddu
    नहीं नही मत अभी बाद में ಬೇಡ ಇಲ್ಲ வேண்டாம் இல்லை వద్దు లేదు""".split()
)
_NEGATIVE_CUES = ("no", "nope", "nah", "not", "dont", "never", "skip", "later", "nahi", "nahin", "mat", "beda", "illa", "vendam", "ledu", "vaddu", "नहीं", "नही", "मत", "ಬೇಡ", "ಇಲ್ಲ", "வேண்டாம்", "இல்லை", "వద్దు", "లేదు")

_PROMPT = (
    "A shop owner was asked by voice: 'Shall I post this campaign on your social media for you?'. "
    "Their spoken reply may be in English or any Indian language. Return ONLY one JSON object: "
    '{"intent": "yes"|"no"|"unclear"}. yes = they want it posted; no = they do not, or want to '
    "do it themselves or later; unclear = anything else."
)


def quick_yes_no(text: str) -> str | None:
    """`yes` / `no` when the reply is unmistakable, else None."""
    if quick_intent(text) == "confirm":
        return "yes"
    words = [w for w in re.split(r"[\s,.!?;:।\-–—\"“”‘’]+", text.lower().replace("'", "")) if w]
    if words and len(words) <= 10 and all(w in _NEGATIVE for w in words) and any(w in _NEGATIVE_CUES for w in words):
        return "no"
    return None


class ConnectIn(BaseModel):
    #: Where the hosted connect page sends the owner back to.
    redirect_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://")


class PostIn(BaseModel):
    platforms: list[str] | None = Field(default=None, max_length=5)


@router.get("/social/status", response_model=dict)
def social_status(user: User = Depends(current_user)) -> dict:
    return social_service.status(user)


@router.post("/social/connect", response_model=dict)
def social_connect(payload: ConnectIn, user: User = Depends(current_user)) -> dict:
    return social_service.connect_link(user, payload.redirect_url)


@router.post("/campaigns/{campaign_id}/social/post", response_model=dict)
def social_post(
    campaign_id: int,
    payload: PostIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    campaign = owned_campaign(db, campaign_id, user)
    result = social_service.post_campaign(db, campaign, user, payload.platforms if payload else None)
    db.commit()
    return result


@router.post("/campaigns/{campaign_id}/publish-turn", response_model=dict)
async def publish_turn(
    campaign_id: int,
    payload: VoiceTurnIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Read a spoken yes/no to "shall I post this for you?". Posts nothing."""
    settings = get_settings()
    campaign = owned_campaign(db, campaign_id, user)
    audio = _decode_audio(payload.audio_b64, settings.max_audio_bytes)

    def _result(intent: str, heard: str, reply: str) -> dict:
        _record_audit(db, campaign, "voice.publish_turn", {"intent": intent, "heard": heard[:500]})
        db.commit()
        return {"intent": intent, "heard": heard, "reply": reply}

    try:
        stt = await get_stt_provider(settings).transcribe(
            audio, mime=payload.audio_mime, language_hint=payload.language_hint
        )
    except (STTUnavailableError, ProviderUnavailableError) as exc:
        return _result("unclear", "", f"I could not hear that: {getattr(exc, 'message', exc)}")
    heard = (stt.raw_transcript or "").strip()
    if not heard:
        return _result("unclear", "", "I did not catch anything. Say “yes” or “no”.")

    intent = quick_yes_no(heard)
    if intent is None and not settings.is_mock:
        try:
            data = parse_llm_json(
                await get_llm_client(settings).complete_json(_PROMPT, heard, max_tokens=60, temperature=0.0)
            )
            intent = str(data.get("intent") or "").lower()
        except TitanError:
            intent = None
    if intent == "yes":
        return _result("yes", heard, "Posting it for you.")
    if intent == "no":
        return _result("no", heard, "No problem — nothing was posted.")
    return _result("unclear", heard, "Say “yes” to post it, or “no” to finish.")
