"""Publisher endpoints (Phase 8).

The owner bound to a publication is the authenticated account, never a value
from the request body: approval is the step that authorizes an outward-facing
action, so its binding has to be trustworthy.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_campaign, owned_publish_record
from ..models import User
from ..services import publisher_service

router = APIRouter(prefix="/campaigns", tags=["publishing"])


class PrepareRequest(BaseModel):
    asset_id: int
    platform: str = Field(pattern="^(instagram|facebook|x|whatsapp|sandbox)$")
    media_kind: str = Field(pattern="^(image|video|text)$")


@router.get("/{campaign_id}/publish/capabilities")
def capabilities(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    return publisher_service.list_actions(db, campaign_id)


@router.post("/{campaign_id}/publish/prepare", status_code=201, response_model=dict)
def prepare(
    campaign_id: int,
    payload: PrepareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    record = publisher_service.prepare(
        db,
        campaign_id=campaign_id,
        asset_id=payload.asset_id,
        platform=payload.platform,
        media_kind=payload.media_kind,
        owner_uid=user.owner_uid,
    )
    db.commit()
    return record


@router.post("/publish/{publish_id}/approve", response_model=dict)
def approve(
    publish_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_publish_record(db, publish_id, user)
    record = publisher_service.approve(db, publish_id)
    db.commit()
    return record


@router.post("/publish/{publish_id}/execute", response_model=dict)
def execute(
    publish_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_publish_record(db, publish_id, user)
    record = publisher_service.execute(db, publish_id)
    db.commit()
    return record


@router.get("/{campaign_id}/publish/records", response_model=list[dict])
def list_records(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    """Every prepared/approved/published record for the campaign."""
    owned_campaign(db, campaign_id, user)
    return publisher_service.list_records(db, campaign_id)


@router.get("/{campaign_id}/publish/export")
def export_campaign(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    owned_campaign(db, campaign_id, user)
    return publisher_service.export_package(db, campaign_id)
