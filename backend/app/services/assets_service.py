"""Asset generation for posters/captions (Phase 4) with full provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import AssetRecord, AuditEvent, Campaign, FactSheetRecord
from ..services.campaign_brain import get_plan_record, substitute_copy
from ..services.checksums import sha256_digest, sha256_text
from ..services.fact_engine import substitute_tokens
from ..services.poster import compose_poster


def _audit(db: Session, campaign_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
    db.add(AuditEvent(campaign_id=campaign_id, event_type=event_type, payload_json=json.dumps(payload) if payload else None))


@dataclass
class GeneratedAsset:
    record: AssetRecord
    bytes_len: int | None = None


def tokens_for_campaign(db: Session, campaign_id: int) -> dict[str, str]:
    sheet = (
        db.query(FactSheetRecord)
        .filter(FactSheetRecord.campaign_id == campaign_id, FactSheetRecord.status == "locked")
        .order_by(FactSheetRecord.version.desc())
        .first()
    )
    if sheet is None:
        raise ConflictError("Campaign facts are not locked.", code="facts_not_locked")
    return json.loads(sheet.tokens_json or "{}")


def poster_art_prompt(plan, tokens: dict[str, str], index: int) -> str:
    """The Director's scene brief, grounded in the locked facts.

    Only descriptive facts (what is sold, to whom, where) reach the image
    model. Numbers, prices and dates never do — the compositor draws those.
    """
    briefs = [b for b in plan.poster_briefs if b.get("art_prompt")]
    base = str(briefs[index % len(briefs)]["art_prompt"]).strip() if briefs else ""
    parts = [base] if base else []
    if tokens.get("PRODUCT"):
        parts.append(f"Featured: {tokens['PRODUCT']}.")
    if tokens.get("AUDIENCE"):
        parts.append(f"Customers shown: {tokens['AUDIENCE']}.")
    if tokens.get("LOCATION"):
        parts.append(f"Setting: {tokens['LOCATION']}, India.")
    return " ".join(parts)


def generate_posters(
    db: Session, campaign_id: int, *, variants: int = 1, allow_fallback_art: bool = False
) -> list[AssetRecord]:
    """Generate poster assets bound to the latest plan + locked tokens."""
    settings = get_settings()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}")
    from ..services.campaign_brain import _plan_from_record

    plan = _plan_from_record(plan_record, tokens)

    existing_kind_count = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id, AssetRecord.kind == "poster")
        .count()
    )
    if existing_kind_count >= settings.max_posters:
        raise ConflictError(
            f"Poster budget reached ({settings.max_posters}).", code="budget_exceeded"
        )

    business_name = (json.loads(sheet.facts_json or "{}").get("business") or {}).get("name") or "Our Shop"
    headline_t = plan.copy.get("poster_headline")
    subline_t = plan.copy.get("poster_subline")
    if headline_t is None:
        raise ValidationError("Plan has no poster_headline template.", code="plan_incomplete")

    # Live mode: the Brag Director art-directs the poster. Its storyboard is
    # fetched on a worker thread so it overlaps the (slower) image generation.
    storyboard = None
    pool = None
    if not settings.is_mock:
        from concurrent.futures import ThreadPoolExecutor

        from . import brag_renderer
        from .brag_service import get_or_create_storyboard

        if brag_renderer.renderer_available():
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(
                get_or_create_storyboard,
                campaign_id,
                plan_record.id,
                tokens,
                business_name=business_name,
                plan_angle=str((plan.strategy or {}).get("angle") or "") or None,
            )
            storyboard = future.result
            pool.shutdown(wait=False)

    created: list[AssetRecord] = []
    for index in range(min(variants, max(1, settings.max_posters - existing_kind_count))):
        headline = substitute_tokens(headline_t.template, tokens)
        subline = substitute_tokens(subline_t.template, tokens) if subline_t else ""
        composition = compose_poster(
            business_name=business_name,
            headline=headline,
            subline=subline,
            tokens=tokens,
            fact_lines={"DISCOUNT": tokens.get("DISCOUNT", ""), "DAYS": tokens.get("DAYS", ""), "WINDOW": tokens.get("WINDOW", "")},
            settings=settings,
            art_prompt=poster_art_prompt(plan, tokens, existing_kind_count + index),
            allow_fallback_art=allow_fallback_art,
            storyboard=storyboard,
        )
        # Numbered after the posters that already exist, so a second batch
        # never overwrites the file an earlier asset row points at.
        relative_path = (
            f"campaigns/{campaign_id}/posters/"
            f"poster_{plan_record.id}_{existing_kind_count + index + 1}.png"
        )
        from ..storage import get_storage

        get_storage().save_bytes(composition.png_bytes, relative_path)
        provenance = composition.metadata()
        if composition.art_bytes:
            # The artwork without text, for the campaign video to reuse.
            art_path = relative_path.replace("/poster_", "/art_")
            get_storage().save_bytes(composition.art_bytes, art_path)
            provenance["art_path"] = art_path

        record = AssetRecord(
            campaign_id=campaign_id,
            plan_id=plan_record.id,
            factsheet_id=sheet.id,
            fact_hash=sheet.fact_hash,
            kind="poster",
            locale="en-IN",
            storage_path=relative_path,
            sha256=sha256_digest(composition.png_bytes),
            asset_status="validating",
            provider=composition.art_provider,
            model=composition.art_model,
            is_mock=composition.is_mock_art,
            used_fallback=composition.used_fallback_art,
            provenance_json=json.dumps(provenance, ensure_ascii=False),
        )
        db.add(record)
        db.flush()
        created.append(record)
        _audit(db, campaign_id, "asset.poster_composed", {"asset_id": record.id, "layout_version": composition.registry["layout_version"], "used_fallback_art": composition.used_fallback_art})
    return created


def generate_captions(db: Session, campaign_id: int) -> list[AssetRecord]:
    """Materialize substituted captions as text assets for Guardian checking."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    plan_record = get_plan_record(db, campaign_id)
    if plan_record is None:
        raise ConflictError("Generate the campaign plan first (POST /plan).", code="plan_missing")
    sheet = db.get(FactSheetRecord, plan_record.fact_sheet_id)
    tokens = json.loads(sheet.tokens_json or "{}")
    from ..services.campaign_brain import _plan_from_record

    plan = _plan_from_record(plan_record, tokens)
    substituted = substitute_copy(plan.copy, tokens, plan.localization)

    created: list[AssetRecord] = []
    for channel, text in substituted["master"].items():
        record = AssetRecord(
            campaign_id=campaign_id,
            plan_id=plan_record.id,
            factsheet_id=sheet.id,
            fact_hash=sheet.fact_hash,
            kind="caption",
            locale="en-IN",
            text_content=text,
            template=(plan.copy.get(channel).template if plan.copy.get(channel) else None),
            sha256=sha256_text(text),
            asset_status="validating",
            provider="deterministic_substitution",
            is_mock=plan.is_mock,
            provenance_json=json.dumps({"channel": channel, "token_map": tokens}, ensure_ascii=False),
        )
        db.add(record)
        created.append(record)
    for language, templates in substituted["localized"].items():
        locale = next((loc.locale for loc in plan.localization if loc.language == language), "en-IN")
        for channel, text in templates.items():
            record = AssetRecord(
                campaign_id=campaign_id,
                plan_id=plan_record.id,
                factsheet_id=sheet.id,
                fact_hash=sheet.fact_hash,
                kind="caption",
                locale=locale,
                text_content=text,
                template=plan.localization[0].copy_templates.get(channel) if plan.localization else None,
                sha256=sha256_text(text),
                asset_status="validating",
                provider="deterministic_substitution",
                is_mock=plan.is_mock,
                provenance_json=json.dumps({"channel": channel, "language": language}, ensure_ascii=False),
            )
            db.add(record)
            created.append(record)
    db.flush()
    _audit(db, campaign_id, "asset.captions_materialized", {"count": len(created)})
    return created


def serialize_asset(record: AssetRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "campaign_id": record.campaign_id,
        "kind": record.kind,
        "locale": record.locale,
        "asset_status": record.asset_status,
        "text_content": record.text_content,
        "storage_path": record.storage_path,
        "sha256": record.sha256,
        "factsheet_id": record.factsheet_id,
        "fact_hash": record.fact_hash,
        "provider": record.provider,
        "model": record.model,
        "is_mock": record.is_mock,
        "used_fallback": record.used_fallback,
        "provenance": json.loads(record.provenance_json) if record.provenance_json else None,
        "created_at": str(record.created_at),
    }
