"""Campaign visuals from the Brag Director: storyboard storage + the brag video.

The storyboard is produced once per campaign plan and kept beside the
campaign's files, so the poster and the video share one art direction and a
second request never pays for (or waits on) another agent call.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import AssetRecord, AuditEvent, Campaign, FactSheetRecord
from ..storage import get_storage
from . import brag_director, brag_renderer
from .campaign_brain import _plan_from_record, get_plan_record
from .checksums import sha256_digest
from .fact_engine import substitute_tokens

BRAG_PROVIDER = "brag_director"


def _storyboard_path(campaign_id: int, plan_id: int) -> str:
    return f"campaigns/{campaign_id}/brag/storyboard_{plan_id}.json"


def load_storyboard(campaign_id: int, plan_id: int) -> dict[str, Any] | None:
    storage = get_storage()
    path = _storyboard_path(campaign_id, plan_id)
    if not storage.exists(path):
        return None
    try:
        return json.loads(storage.read_bytes(path).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def get_or_create_storyboard(
    campaign_id: int,
    plan_id: int,
    tokens: dict[str, str],
    *,
    business_name: str | None,
    plan_angle: str | None,
) -> dict[str, Any]:
    """Thread-safe by construction: touches storage only, never the DB session."""
    board = load_storyboard(campaign_id, plan_id)
    if board is not None:
        return board
    board = brag_director.direct(tokens, business_name=business_name, plan_angle=plan_angle)
    get_storage().save_bytes(
        json.dumps(board, ensure_ascii=False, indent=1).encode("utf-8"),
        _storyboard_path(campaign_id, plan_id),
    )
    return board


def create_brag_video(db: Session, campaign_id: int) -> AssetRecord:
    """Render the storyboard to a vertical video (frames → FFmpeg) and store it."""
    settings = get_settings()
    if db.get(Campaign, campaign_id) is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}") if sheet else {}
    if not tokens.get("PRODUCT"):
        raise ValidationError("The locked facts have no product to show.", code="plan_incomplete")
    plan = _plan_from_record(plan_record, tokens)

    assets = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id)
        .order_by(AssetRecord.id.desc())
        .all()
    )
    made = [a for a in assets if a.kind == "video" and a.provider == BRAG_PROVIDER]
    if len(made) >= settings.max_video_jobs:
        raise ConflictError(f"Video budget reached ({settings.max_video_jobs}).", code="budget_exceeded")

    storage = get_storage()
    business = (json.loads(sheet.facts_json or "{}").get("business") or {}).get("name") or "Our Shop"
    headline_template = plan.copy.get("poster_headline")
    headline = (
        substitute_tokens(headline_template.template, tokens) if headline_template else tokens["PRODUCT"]
    )

    # The poster's raw artwork (no text on it) becomes the video's imagery.
    art: bytes | None = None
    poster = next((a for a in assets if a.kind == "poster"), None)
    if poster is not None:
        art_path = json.loads(poster.provenance_json or "{}").get("art_path")
        if art_path and storage.exists(art_path):
            art = storage.read_bytes(art_path)

    voice = next((a for a in assets if a.kind == "voice" and a.storage_path and not a.is_mock), None)
    voice_wav = storage.read_bytes(voice.storage_path) if voice is not None else None

    board = get_or_create_storyboard(
        campaign_id,
        plan_record.id,
        tokens,
        business_name=business,
        plan_angle=str((plan.strategy or {}).get("angle") or "") or None,
    )
    spec = brag_renderer.build_spec(
        board,
        tokens,
        mode="video",
        business_name=business,
        headline=headline,
        art=art,
        voice_seconds=brag_renderer.wav_seconds(voice_wav),
    )
    data, info = brag_renderer.render_video(spec, voice_wav=voice_wav)

    relative_path = f"campaigns/{campaign_id}/videos/brag_{plan_record.id}_{len(made) + 1}.mp4"
    storage.save_bytes(data, relative_path)
    agent = board.get("agent") or {}
    record = AssetRecord(
        campaign_id=campaign_id,
        plan_id=plan_record.id,
        factsheet_id=sheet.id,
        fact_hash=sheet.fact_hash,
        kind="video",
        locale=voice.locale if voice is not None else "en-IN",
        storage_path=relative_path,
        sha256=sha256_digest(data),
        asset_status="validating",
        provider=BRAG_PROVIDER,
        model=agent.get("model") or "default-storyboard",
        is_mock=False,
        used_fallback=bool(agent.get("used_default")),
        provenance_json=json.dumps(
            {
                "engine": "brag_frames",
                "storyboard": board,
                "registry": {"rendered_facts": info["rendered"]},
                "seconds": info["seconds"],
                "frames": info["frames"],
                "fps": info["fps"],
                "voice_asset_id": voice.id if voice is not None else None,
                "poster_asset_id": poster.id if poster is not None else None,
                "art": "poster_artwork" if art else "none",
                "playable": True,
            },
            ensure_ascii=False,
        ),
    )
    db.add(record)
    db.flush()
    db.add(
        AuditEvent(
            campaign_id=campaign_id,
            event_type="video.brag_rendered",
            payload_json=json.dumps({"asset_id": record.id, "seconds": info["seconds"], "agent": agent}),
        )
    )
    return record
