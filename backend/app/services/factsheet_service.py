"""Fact sheet persistence and orchestration helpers.

Keeps the routers thin and encodes the versioning / lock invariants in one
place:

- a draft is mutable;
- a locked version is immutable;
- editing a locked version creates a new draft version and marks the old one
  `superseded`.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..errors import (
    ConflictError,
    IncompleteFactSheetError,
    NotFoundError,
    SealUnavailableError,
    ValidationError,
)
from ..models import (
    AuditEvent,
    Campaign,
    CampaignStatus,
    FactSheetRecord,
    FactSheetStatus,
    utcnow,
)
from ..schemas import ExtractionStatus, FactSheetRead, FactSheetVersionSummary
from .fact_engine import (
    SEAL_ALGORITHM,
    canonical_json,
    canonical_payload,
    compile_tokens,
    compute_fact_hash,
    compute_seal,
    lock_blockers,
    normalize_fact_data,
    verify_locked_payload,
)
from .fact_schemas import FactSheet, FactSheetPatch
from .fact_validation import blocking_findings, refresh_deterministic, stored_validation


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(value: str | None) -> Any:
    return json.loads(value) if value else None


def audit(db: Session, campaign_id: int, event_type: str, payload: dict | None = None) -> None:
    db.add(
        AuditEvent(
            campaign_id=campaign_id,
            event_type=event_type,
            payload_json=_dump(payload) if payload is not None else None,
        )
    )


# ── queries ───────────────────────────────────────────────────────────
def get_sheet(db: Session, sheet_id: int) -> FactSheetRecord:
    sheet = db.get(FactSheetRecord, sheet_id)
    if sheet is None:
        raise NotFoundError(
            f"FactSheet {sheet_id} was not found.", details={"factsheet_id": sheet_id}
        )
    return sheet


def latest_sheet(db: Session, campaign_id: int) -> FactSheetRecord | None:
    return db.scalar(
        select(FactSheetRecord)
        .where(FactSheetRecord.campaign_id == campaign_id)
        .order_by(FactSheetRecord.version.desc())
        .limit(1)
    )


def list_versions(db: Session, campaign_id: int) -> list[FactSheetRecord]:
    return list(
        db.scalars(
            select(FactSheetRecord)
            .where(FactSheetRecord.campaign_id == campaign_id)
            .order_by(FactSheetRecord.version)
        ).all()
    )


def _next_version(db: Session, campaign_id: int) -> int:
    current = db.scalar(
        select(func.max(FactSheetRecord.version)).where(
            FactSheetRecord.campaign_id == campaign_id
        )
    )
    return (current or 0) + 1


# ── serialization ─────────────────────────────────────────────────────
def serialize_sheet(
    sheet: FactSheetRecord, settings: Settings | None = None
) -> FactSheetRead:
    settings = settings or get_settings()
    facts = FactSheet.model_validate(_load(sheet.draft_json) or {})

    tokens: dict[str, str] | None = None
    seal_valid: bool | None = None
    seal_algorithm: str | None = None
    if sheet.seal and sheet.facts_json:
        # Locked *and* superseded versions retain a verifiable seal.
        tokens = _load(sheet.tokens_json) if sheet.tokens_json else None
        seal_algorithm = SEAL_ALGORITHM
        payload = _load(sheet.facts_json)
        if payload is not None:
            seal_valid = verify_locked_payload(
                payload, sheet.fact_hash, sheet.seal, settings.seal_secret
            )

    extraction = None
    raw_extraction = _load(sheet.extraction_json)
    if raw_extraction and raw_extraction.get("status"):
        extraction = ExtractionStatus(
            **{k: v for k, v in raw_extraction.items() if k in ExtractionStatus.model_fields}
        )

    return FactSheetRead(
        id=sheet.id,
        campaign_id=sheet.campaign_id,
        version=sheet.version,
        status=sheet.status,
        facts=facts,
        tokens=tokens,
        fact_hash=sheet.fact_hash,
        seal=sheet.seal,
        seal_algorithm=seal_algorithm,
        seal_valid=seal_valid,
        extraction=extraction,
        validation=stored_validation(sheet),
        created_at=sheet.created_at,
        updated_at=sheet.updated_at,
        locked_at=sheet.locked_at,
    )


def version_summary(sheet: FactSheetRecord) -> FactSheetVersionSummary:
    return FactSheetVersionSummary(
        id=sheet.id,
        version=sheet.version,
        status=sheet.status,
        fact_hash=sheet.fact_hash,
        created_at=sheet.created_at,
        locked_at=sheet.locked_at,
    )


# ── extraction persistence ────────────────────────────────────────────
def record_extraction(
    db: Session,
    campaign: Campaign,
    *,
    extracted: FactSheet | None,
    extraction: dict[str, Any],
    fallback_facts: FactSheet,
) -> FactSheetRecord:
    """Create or update the campaign's active draft with extraction output.

    If the latest version is a draft it is reused (a draft is mutable). If the
    latest version is locked, a new draft version is created and the locked one
    is marked superseded. `extracted=None` means the provider was unavailable:
    existing draft facts are preserved, otherwise an empty (truthful) sheet is
    stored for manual entry.
    """
    latest = latest_sheet(db, campaign.id)

    if latest is not None and latest.status == FactSheetStatus.DRAFT:
        target = latest
        if extracted is not None:
            target.draft_json = _dump(extracted.model_dump(mode="json"))
        target.extraction_json = _dump(extraction)
        audit(db, campaign.id, "facts.extracted", {"version": target.version, "status": extraction.get("status")})
    else:
        if latest is not None and latest.status == FactSheetStatus.LOCKED:
            latest.status = FactSheetStatus.SUPERSEDED
            db.flush()
        facts = extracted if extracted is not None else fallback_facts
        target = FactSheetRecord(
            campaign_id=campaign.id,
            version=_next_version(db, campaign.id),
            status=FactSheetStatus.DRAFT,
            parent_id=latest.id if latest else None,
            draft_json=_dump(facts.model_dump(mode="json")),
            extraction_json=_dump(extraction),
        )
        db.add(target)
        db.flush()
        audit(
            db,
            campaign.id,
            "facts.extracted",
            {"version": target.version, "status": extraction.get("status")},
        )

    campaign.status = CampaignStatus.EXTRACTED
    db.commit()
    return target


# ── human edits ───────────────────────────────────────────────────────
def _merge_patch(
    base: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    changed: list[str] = []
    for section in ("business", "offer"):
        if section in patch and isinstance(patch[section], dict):
            base.setdefault(section, {})
            for key, value in patch[section].items():
                if base[section].get(key) != value:
                    changed.append(f"{section}.{key}")
                base[section][key] = value
    if "languages" in patch:
        if base.get("languages") != patch["languages"]:
            changed.append("languages")
        base["languages"] = patch["languages"]
    return base, changed


def _strip_human_metadata(sheet: FactSheet, changed: list[str]) -> FactSheet:
    """A human-entered value is no longer model-inferred or model-confident."""
    if not changed:
        return sheet
    changed_set = set(changed)
    return sheet.model_copy(
        update={
            "inferred": [p for p in sheet.inferred if p not in changed_set],
            "extraction_confidence": {
                k: v
                for k, v in sheet.extraction_confidence.items()
                if k not in changed_set
            },
        }
    )


def apply_patch(
    db: Session,
    sheet: FactSheetRecord,
    patch: FactSheetPatch,
) -> FactSheetRecord:
    if sheet.status == FactSheetStatus.SUPERSEDED:
        raise ConflictError(
            "This FactSheet version is superseded and cannot be edited. "
            "Edit the active version instead.",
            code="superseded_version",
            details={"factsheet_id": sheet.id},
        )

    patch_data = patch.model_dump(exclude_unset=True, mode="python")
    if not patch_data:
        raise ValidationError("The FactSheet patch contains no changes.", code="empty_patch")

    base = _load(sheet.draft_json) or {}
    merged, changed = _merge_patch(base, patch_data)
    if not changed:
        raise ValidationError("The FactSheet patch contains no changes.", code="empty_patch")

    normalized = normalize_fact_data(merged)
    normalized = _strip_human_metadata(normalized, changed)

    campaign = db.get(Campaign, sheet.campaign_id)
    if sheet.status == FactSheetStatus.LOCKED:
        sheet.status = FactSheetStatus.SUPERSEDED
        db.flush()
        new_sheet = FactSheetRecord(
            campaign_id=sheet.campaign_id,
            version=_next_version(db, sheet.campaign_id),
            status=FactSheetStatus.DRAFT,
            parent_id=sheet.id,
            draft_json=_dump(normalized.model_dump(mode="json")),
            extraction_json=sheet.extraction_json,
        )
        db.add(new_sheet)
        db.flush()
        audit(
            db,
            sheet.campaign_id,
            "factsheet.version_created",
            {"from_locked": sheet.id, "version": new_sheet.version, "changed": changed},
        )
        target = new_sheet
    else:
        sheet.draft_json = _dump(normalized.model_dump(mode="json"))
        audit(db, sheet.campaign_id, "factsheet.edited", {"version": sheet.version, "changed": changed})
        target = sheet

    # Deterministic validation follows every edit; a previous Agnes report is
    # kept but marked stale until it is re-run against the edited facts.
    refresh_deterministic(target)

    if campaign is not None:
        campaign.status = CampaignStatus.EXTRACTED
    db.commit()
    return target


# ── locking ───────────────────────────────────────────────────────────
def lock_sheet(
    db: Session, sheet: FactSheetRecord, settings: Settings | None = None
) -> FactSheetRecord:
    settings = settings or get_settings()

    if sheet.status == FactSheetStatus.SUPERSEDED:
        raise ConflictError(
            "This FactSheet version is superseded and cannot be locked.",
            code="superseded_version",
            details={"factsheet_id": sheet.id},
        )
    # Locking an already-locked version is idempotent.
    if sheet.status == FactSheetStatus.LOCKED:
        return sheet

    if not settings.seal_configured:
        raise SealUnavailableError(
            "Fact locking is disabled: TITAN_SEAL_SECRET is not configured.",
            details={"configured": False},
        )

    current = FactSheet.model_validate(_load(sheet.draft_json) or {})
    normalized = normalize_fact_data(current.model_dump(mode="json"))

    blockers = lock_blockers(normalized)
    if blockers:
        raise IncompleteFactSheetError(
            "Cannot lock: required facts are missing.",
            details={"missing": blockers},
        )

    # Only facts that pass deterministic validation may be locked. Agnes'
    # semantic findings are advisory and are confirmed by the owner locking.
    invalid = blocking_findings(normalized)
    if invalid:
        raise ValidationError(
            "Cannot lock: " + " ".join(item["message"] for item in invalid),
            code="facts_invalid",
            details={"findings": invalid},
        )

    payload = canonical_payload(normalized)
    canonical = canonical_json(payload)
    fact_hash = compute_fact_hash(canonical)
    seal = compute_seal(canonical, settings.seal_secret)
    tokens = compile_tokens(payload)

    sheet.draft_json = _dump(normalized.model_dump(mode="json"))
    refresh_deterministic(sheet)
    sheet.facts_json = canonical
    sheet.tokens_json = _dump(tokens)
    sheet.fact_hash = fact_hash
    sheet.seal = seal
    sheet.status = FactSheetStatus.LOCKED
    sheet.locked_at = utcnow()

    campaign = db.get(Campaign, sheet.campaign_id)
    if campaign is not None:
        campaign.status = CampaignStatus.LOCKED

    audit(
        db,
        sheet.campaign_id,
        "facts.locked",
        {"version": sheet.version, "fact_hash": fact_hash, "tokens": sorted(tokens)},
    )
    db.commit()
    return sheet
