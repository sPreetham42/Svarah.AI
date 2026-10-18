"""Guardian API endpoints (Phase 7): verify, repair, certificate, sabotage demo."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import current_user, owned_asset, owned_campaign
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import AssetRecord, User, VerificationResultRecord
from ..services.assets_service import serialize_asset
from ..services.factsheet_service import audit
from ..guardian import certificate as cert_module
from ..guardian.runner import (
    AGGREGATE_CHECK,
    ASSET_STATUS_FAILED,
    ASSET_STATUS_VERIFIED,
    VERDICT_FAIL,
    repair_asset,
    verify_asset,
)

router = APIRouter(tags=["guardian"])

#: Asset status set by an explicit, recorded human decision (SoT §34).
ASSET_STATUS_HUMAN_VERIFIED = "human_verified"


@router.post("/campaigns/{campaign_id}/verify", response_model=list[dict])
def verify_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    owned_campaign(db, campaign_id, user)
    records = db.scalars(select(AssetRecord).where(AssetRecord.campaign_id == campaign_id).order_by(AssetRecord.id)).all()
    reports = []
    for record in records:
        report = verify_asset(db, record)
        reports.append(report.as_dict())
    db.commit()
    if not reports:
        raise ConflictError("No assets to verify for this campaign.", code="no_assets")
    return reports


@router.post("/assets/{asset_id}/repair", response_model=dict)
def repair(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    record = owned_asset(db, asset_id, user)
    result = repair_asset(db, record)
    db.commit()
    return result


# ── human verification (Source of Truth §34) ──────────────────────────
class HumanVerifyRequest(BaseModel):
    attestation: bool = Field(default=False)
    note: str | None = Field(default=None, max_length=500)


@router.post("/assets/{asset_id}/human-verify", response_model=dict)
def human_verify(
    asset_id: int,
    payload: HumanVerifyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Record an explicit owner decision on an *inconclusive* Guardian result.

    Guardian must have run first. A positive failure (an aggregate ``FAIL``)
    is refused: a contradiction with the locked facts is resolved by correcting
    the content and re-verifying, never by overriding it. An inconclusive
    verdict (``NEEDS_REVIEW``/``WARN``) may be accepted by the owner, who takes
    responsibility for that call.

    The Guardian result rows are left in place, so the verification certificate
    and the audit trail still report exactly what the machine found — the human
    decision is recorded alongside it, not instead of it.
    """
    record = owned_asset(db, asset_id, user)
    if not payload.attestation:
        raise ValidationError(
            "Confirm that you have reviewed this asset before recording a human decision.",
            code="attestation_required",
        )
    # Already acceptable to publish — keep the route idempotent.
    if record.asset_status in (ASSET_STATUS_VERIFIED, ASSET_STATUS_HUMAN_VERIFIED):
        return serialize_asset(record)

    rows = db.scalars(
        select(VerificationResultRecord)
        .where(VerificationResultRecord.asset_id == asset_id)
        .order_by(VerificationResultRecord.id)
    ).all()
    # Guardian appends an aggregate row per verification run, so the
    # authoritative verdict is the most recent one — a stale earlier FAIL must
    # not make a since-repaired asset permanently un-acceptable.
    aggregate = next(
        (row for row in reversed(rows) if row.check_name == AGGREGATE_CHECK), None
    )
    if aggregate is None:
        raise ConflictError(
            "Run Guardian verification before recording a human decision.",
            code="verification_required",
        )
    if aggregate.verdict == VERDICT_FAIL or record.asset_status == ASSET_STATUS_FAILED:
        raise ConflictError(
            "This asset failed a Guardian check. Correct the content and re-verify — "
            "a hard failure cannot be overridden by an attestation.",
            code="human_verify_refused",
            details={"verdict": aggregate.verdict},
        )

    previous = record.asset_status
    record.asset_status = ASSET_STATUS_HUMAN_VERIFIED
    audit(
        db,
        record.campaign_id,
        "asset.human_verified",
        {
            "asset_id": record.id,
            "previous_status": previous,
            "guardian_verdict": aggregate.verdict,
            "note": (payload.note or "").strip() or None,
        },
    )
    db.commit()
    return serialize_asset(record)


@router.post("/campaigns/{campaign_id}/certificate")
def issue_certificate(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    certificate = cert_module.build_certificate(db, campaign_id)
    db.commit()
    return certificate


@router.get("/campaigns/{campaign_id}/certificate/html", response_class=HTMLResponse)
def certificate_html(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> str:
    owned_campaign(db, campaign_id, user)
    certificate = cert_module.build_certificate(db, campaign_id)
    return cert_module.certificate_html(certificate)


# ── sabotage demo (development-only, copy-only artifacts) ─────────────
class SabotageRequest(BaseModel):
    mode: str = Field(default="discount", pattern="^(discount|day|claim)$")


@router.post("/demo/sabotage/{asset_id}", response_model=dict)
def sabotage(
    asset_id: int,
    payload: SabotageRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    """Corrupt a COPY of an asset's caption, show Guardian catching it.

    Rules (handoff §5):
    - requires TITAN_ENABLE_DEMO_SABOTAGE=true AND TITAN_MODE=mock;
    - operates ONLY on a copied test artifact — authoritative assets are never
      mutated; source-of-truth records (factsheets/tokens) are untouched;
    - original caption stays intact.
    """
    settings = get_settings()
    if settings.enable_demo_sabotage is False or not settings.is_mock:
        raise ConflictError(
            "Sabotage demo is disabled (enable TITAN_ENABLE_DEMO_SABOTAGE=true in mock mode).",
            code="sabotage_disabled",
        )
    record = owned_asset(db, asset_id, user)
    if record.kind != "caption" or not record.text_content:
        raise NotFoundError(f"Caption asset {asset_id} was not found.")

    text = record.text_content
    original = text
    corrupted = corrupt_text(text, payload.mode)
    if corrupted == original:
        raise ValidationError(
            "Could not apply a corruption to this caption (no target string found).",
            code="sabotage_target_missing",
        )

    test_asset = AssetRecord(
        campaign_id=record.campaign_id,
        plan_id=record.plan_id,
        factsheet_id=record.factsheet_id,
        fact_hash=record.fact_hash,
        kind="caption",
        locale=record.locale,
        text_content=corrupted,
        asset_status="validating",
        provider="sabotage_demo",
        is_mock=True,
        used_fallback=False,
        provenance_json=json.dumps({"copied_from": record.id, "mode": payload.mode, "purpose": "demo"}),
    )
    db.add(test_asset)
    db.flush()
    report = verify_asset(db, test_asset)
    db.flush()
    # The demo copy is deleted after collection — nothing authoritative changed.
    result = {
        "original_asset_id": record.id,
        "original_text": original,
        "corrupted_text": corrupted,
        "guardian_report": report.as_dict(),
        "original_unchanged": True,
    }
    db.delete(test_asset)
    db.commit()
    return result


def corrupt_text(text: str, mode: str) -> str:
    """Deterministic corruption of a COPY: 20→25, Saturday→Sunday, add claim."""
    if mode == "discount":
        return text.replace("20%", "25%") if "20%" in text else text
    if mode == "day":
        return text.replace("Sunday", "Monday") if "Sunday" in text else text
    if mode == "claim":
        return text + " First 50 customers get a free gift!"
    return text
