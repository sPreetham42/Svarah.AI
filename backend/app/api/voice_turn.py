"""Spoken review of the drafted facts (voice-only workspace).

After an offer is captured the owner answers by voice instead of clicking:

- **confirm** — "yes", "looks right", "haan, sahi hai" → the client locks the
  facts and generates the package;
- **correct** — "make it 25 percent", "the shop is Sharma Cafe" → the changed
  fields are applied as an ordinary FactSheet edit (same normalisation and
  validation as a typed edit) and the facts are shown again;
- **restart** — "start over" → the client begins a new offer;
- **unclear** — nothing is changed and the owner is asked to say it again.

Nothing here locks or generates: a spoken "yes" is only *reported*; the lock
endpoint keeps its own blockers. An obvious yes/start-over is recognised
without a model call; everything else is interpreted by the configured text
model, and its patch is accepted only if it passes the FactSheet patch schema.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, ValidationError as PydanticValidationError
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import ConflictError, ProviderUnavailableError, STTUnavailableError, TitanError
from ..models import FactSheetStatus, User
from ..services.agnes import get_llm_client, parse_llm_json
from ..services.fact_schemas import FactSheetPatch
from ..services.fact_validation import validate_sheet
from ..services.factsheet_service import apply_patch, latest_sheet
from ..services.stt import get_stt_provider
from .campaigns import _decode_audio, _record_audit, _serialize

router = APIRouter(prefix="/campaigns", tags=["voice"])

INTENTS = ("confirm", "correct", "restart", "unclear")

#: A reply made only of these words is a plain "yes" (no model call needed).
_AFFIRMATIVE = frozenset(
    """yes yeah yep yup ya ok okay correct right confirm confirmed perfect fine great sure
    absolutely exactly done proceed continue good looks look sounds all everything that
    thats this it its is the go ahead make my posts post please do create generate
    haan han haa ha ji sahi hai theek thik bilkul howdu sari aam avunu sare
    हाँ हां जी सही है ठीक बिल्कुल ಹೌದು ಸರಿ ஆம் சரி అవును సరే""".split()
)
_RESTART_PHRASES = (
    "start over", "start again", "new offer", "cancel this", "discard this", "scrap this",
    "begin again", "naya offer", "फिर से शुरू",
)

_SYSTEM_PROMPT = (
    "You interpret a shop owner's SPOKEN reply while they review the facts extracted from "
    "their offer. The reply may be in English or any Indian language. Return ONLY one JSON "
    'object: {"intent": "confirm"|"correct"|"restart"|"unclear", "patch": {...}, "reply": "..."}.\n'
    "- confirm: they agree the facts are right and ask for no change.\n"
    "- correct: they change, add or remove a fact. Put ONLY the changed fields in `patch`, "
    'shaped as {"business": {"name", "location"}, "offer": {"product": [..], '
    '"discount_percent": number, "discount_flat": number, "price": number, "quantity": number, '
    '"audience": [..], "days": [full English weekday names], "date_start": "YYYY-MM-DD", '
    '"date_end": "YYYY-MM-DD", "start_time": "HH:MM" (24h), "end_time": "HH:MM", '
    '"conditions": [..], "location": string}, "languages": [English language names]}. '
    "A list replaces the whole list, so repeat the items that should stay. Use null to "
    "clear a value. Translate product names and places to English only when the speaker "
    "used a common word; keep proper names as spoken.\n"
    "- restart: they want to throw this offer away and begin a new one.\n"
    "- unclear: anything else, including silence or unrelated speech.\n"
    "NEVER put a value in the patch that the speaker did not say. "
    "`reply` is one short English sentence saying what you changed or what you need."
)


class VoiceTurnIn(BaseModel):
    audio_b64: str
    audio_mime: str | None = Field(default=None, max_length=128)
    language_hint: str | None = Field(default=None, max_length=16)


def quick_intent(text: str) -> str | None:
    """`confirm` / `restart` when the reply is unmistakable, else None."""
    lowered = text.lower().strip()
    if any(phrase in lowered for phrase in _RESTART_PHRASES):
        return "restart"
    # Split on spaces and punctuation only: Indic vowel signs are combining
    # marks, so a `\w+` tokenizer would cut words like "हाँ" in half.
    words = [w for w in re.split(r"[\s,.!?;:।\-–—\"“”‘’]+", lowered.replace("'", "")) if w]
    if words and len(words) <= 10 and all(word in _AFFIRMATIVE for word in words):
        return "confirm"
    return None


async def _interpret(heard: str, facts: dict, settings: Settings) -> tuple[str, dict, str]:
    """(intent, patch, reply) from the text model. Raises ProviderUnavailableError."""
    result = await get_llm_client(settings).complete_json(
        _SYSTEM_PROMPT,
        json.dumps(
            {
                "current_facts": {k: facts.get(k) for k in ("business", "offer", "languages")},
                "spoken_reply": heard,
            },
            ensure_ascii=False,
        ),
        max_tokens=700,
        temperature=0.0,
    )
    data = parse_llm_json(result)
    intent = str(data.get("intent") or "").lower()
    if intent not in INTENTS:
        intent = "unclear"
    patch = data.get("patch") if isinstance(data.get("patch"), dict) else {}
    return intent, patch, str(data.get("reply") or "")[:300]


@router.post("/{campaign_id}/voice-turn", response_model=dict)
async def voice_turn(
    campaign_id: int,
    payload: VoiceTurnIn,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    settings = get_settings()
    campaign = owned_campaign(db, campaign_id, user)
    sheet = latest_sheet(db, campaign.id)
    if sheet is None:
        raise ConflictError("This offer has no facts to review yet.", code="factsheet_missing")

    audio = _decode_audio(payload.audio_b64, settings.max_audio_bytes)

    def _result(intent: str, heard: str, reply: str, changed: list[str] | None = None) -> dict:
        _record_audit(
            db, campaign, "voice.turn", {"intent": intent, "heard": heard[:500], "changed": changed or []}
        )
        db.commit()
        return {
            "intent": intent,
            "heard": heard,
            "reply": reply,
            "changed": changed or [],
            "campaign": _serialize(db, campaign).model_dump(mode="json"),
        }

    try:
        stt = await get_stt_provider(settings).transcribe(
            audio, mime=payload.audio_mime, language_hint=payload.language_hint
        )
    except (STTUnavailableError, ProviderUnavailableError) as exc:
        return _result("unclear", "", f"I could not hear that: {getattr(exc, 'message', exc)}")
    heard = (stt.raw_transcript or "").strip()
    if not heard:
        return _result("unclear", "", "I did not catch anything. Hold the mic and say it again.")

    intent = quick_intent(heard)
    if intent == "confirm":
        return _result("confirm", heard, "Confirmed.")
    if intent == "restart":
        return _result("restart", heard, "Starting a new offer.")

    if settings.is_mock:
        # The offline text model cannot interpret speech; only a plain yes or
        # "start over" is understood in mock mode.
        return _result("unclear", heard, "Say “yes” to confirm, or “start over”.")

    facts = json.loads(sheet.draft_json or "{}")
    try:
        intent, patch, reply = await _interpret(heard, facts, settings)
    except TitanError as exc:
        return _result("unclear", heard, f"I could not work that out: {exc.message}")

    if intent != "correct":
        return _result(intent, heard, reply)
    if sheet.status == FactSheetStatus.SUPERSEDED:
        raise ConflictError("This FactSheet version is superseded.", code="superseded_version")
    try:
        parsed = FactSheetPatch.model_validate(patch)
    except PydanticValidationError:
        return _result("unclear", heard, "I could not turn that into a change. Say it again, one detail at a time.")
    before = json.loads(sheet.draft_json or "{}")
    try:
        target = apply_patch(db, sheet, parsed)
    except TitanError as exc:
        return _result("unclear", heard, exc.message)

    after = json.loads(target.draft_json or "{}")
    changed = sorted(
        f"{group}.{key}"
        for group in ("business", "offer")
        for key in (after.get(group) or {})
        if (after.get(group) or {}).get(key) != (before.get(group) or {}).get(key)
    )
    if after.get("languages") != before.get("languages"):
        changed.append("languages")
    # The correction is part of what the owner said, so the semantic check
    # compares the facts against the offer *and* the correction.
    spoken = f"{campaign.transcript or ''}\nSpoken correction by the owner: {heard}"
    await validate_sheet(target, spoken, settings)
    return _result("correct", heard, reply or "Updated.", changed)
