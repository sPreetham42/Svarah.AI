"""FactSheet endpoints (Source of Truth §46).

Every route resolves the sheet through `owned_factsheet`, so a sheet id alone
never grants access: the caller must own the campaign the sheet belongs to.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_factsheet
from ..config import get_settings
from ..errors import ConflictError
from ..models import Campaign, FactSheetStatus, User
from ..schemas import FactSheetRead
from ..services.fact_schemas import FactSheetPatch
from ..services.fact_validation import validate_sheet
from ..services.factsheet_service import (
    apply_patch,
    lock_sheet,
    serialize_sheet,
)

router = APIRouter(prefix="/factsheets", tags=["factsheets"])


@router.get("/{sheet_id}", response_model=FactSheetRead)
def get_factsheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FactSheetRead:
    return serialize_sheet(owned_factsheet(db, sheet_id, user))


@router.patch("/{sheet_id}", response_model=FactSheetRead)
def edit_factsheet(
    sheet_id: int,
    patch: FactSheetPatch,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FactSheetRead:
    sheet = owned_factsheet(db, sheet_id, user)
    updated = apply_patch(db, sheet, patch)
    return serialize_sheet(updated)


@router.post("/{sheet_id}/lock", response_model=FactSheetRead)
def lock_factsheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FactSheetRead:
    sheet = owned_factsheet(db, sheet_id, user)
    locked = lock_sheet(db, sheet)
    return serialize_sheet(locked)


@router.post("/{sheet_id}/validate", response_model=FactSheetRead)
async def validate_factsheet(
    sheet_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> FactSheetRead:
    """Run deterministic + Agnes semantic validation on the current facts.

    A failed model call is stored as `semantic.status == "unavailable"` with
    the provider's reason — it is never reported as a pass.
    """
    sheet = owned_factsheet(db, sheet_id, user)
    if sheet.status == FactSheetStatus.SUPERSEDED:
        raise ConflictError(
            "This FactSheet version is superseded. Validate the active version instead.",
            code="superseded_version",
            details={"factsheet_id": sheet.id},
        )
    campaign = db.get(Campaign, sheet.campaign_id)
    await validate_sheet(sheet, campaign.transcript if campaign else None, get_settings())
    db.commit()
    return serialize_sheet(sheet)
