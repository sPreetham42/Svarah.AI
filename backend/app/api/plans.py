"""Campaign plan endpoints (Phase 3)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_campaign
from ..errors import NotFoundError
from ..models import Campaign, FactSheetRecord, User
from ..services.campaign_brain import (
    CampaignPlan,
    generate_plan,
    get_plan_record,
    get_locked_sheet,
    substitute_copy,
)
from ..services.fact_engine import verify_locked_payload
from ..config import get_settings

router = APIRouter(prefix="/campaigns", tags=["plans"])

TAMPERED = "Facts failed seal re-verification; refusing to plan against them."


def _sheet_for(db: Session, sheet_id: int | None) -> FactSheetRecord | None:
    return db.get(FactSheetRecord, sheet_id) if sheet_id else None


def _load_plan(db: Session, record) -> CampaignPlan:
    """Deserialize a stored plan using the caller's request session."""
    from ..services.campaign_brain import _plan_from_record

    return _plan_from_record(record, _tokens_for(_sheet_for(db, record.fact_sheet_id)))


def _tokens_for(sheet: FactSheetRecord | None) -> dict[str, str]:
    return json.loads(sheet.tokens_json or "{}") if sheet else {}


class PlanRead(BaseModel):
    id: int | None = None
    campaign_id: int
    fact_sheet_id: int
    fact_hash: str
    version: int
    strategy: dict
    copy_templates: dict
    localization: list[dict]
    poster_briefs: list[dict]
    video_brief: dict = Field(default_factory=dict)
    missing_information: list[str] = Field(default_factory=list)
    brief: dict = Field(default_factory=dict)
    is_mock: bool
    provider: str
    model: str | None = None


class PlanRequest(BaseModel):
    """The owner's brief for the Campaign Director (all optional)."""

    objective: str | None = Field(default=None, max_length=40)
    tone: str | None = Field(default=None, max_length=120)
    instructions: str | None = Field(default=None, max_length=600)
    #: Store a new plan version even when one exists for these locked facts.
    regenerate: bool = False


def _plan_payload(plan: CampaignPlan) -> dict:
    data = plan.as_dict()
    data["copy_templates"] = data.pop("copy")
    return {k: v for k, v in data.items() if k in PlanRead.model_fields}


@router.post("/{campaign_id}/plan", response_model=PlanRead)
def create_plan(
    campaign_id: int,
    body: PlanRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> PlanRead:
    """Ask the Campaign Director for the plan of a locked campaign.

    Idempotent per locked fact version unless `regenerate` is set. In live
    mode a provider or validation failure is returned as an error and nothing
    is stored.
    """
    campaign = owned_campaign(db, campaign_id, user)
    settings = get_settings()

    # Seal-check the locked sheet before planning against it.
    sheet = get_plan_locked_sheet(db, campaign_id)
    if sheet.facts_json and sheet.seal:
        payload = json.loads(sheet.facts_json)
        if verify_locked_payload(payload, sheet.fact_hash, sheet.seal, settings.seal_secret) is False:
            from ..errors import ConflictError

            raise ConflictError(TAMPERED, code="seal_invalid")

    request = body or PlanRequest()
    plan, _ = generate_plan(
        db,
        campaign,
        settings,
        brief=request.model_dump(include={"objective", "tone", "instructions"}, exclude_none=True),
        regenerate=request.regenerate,
    )
    db.commit()
    return PlanRead(**_plan_payload(plan))


@router.get("/{campaign_id}/plan")
def read_plan(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    """Return the plan + substituted copy (master + localized) for the campaign."""
    owned_campaign(db, campaign_id, user)
    record = get_plan_record(db, campaign_id)
    if record is None:
        raise NotFoundError(
            f"Campaign {campaign_id} has no plan yet. POST /plan first.",
            details={"campaign_id": campaign_id},
        )
    plan = _load_plan(db, record)
    tokens = _tokens_for(_sheet_for(db, record.fact_sheet_id))
    return {"plan": plan.as_dict(), "substituted": substitute_copy(plan.copy, tokens, plan.localization)}


def get_plan_locked_sheet(db: Session, campaign_id: int):
    return get_locked_sheet(db, campaign_id)
