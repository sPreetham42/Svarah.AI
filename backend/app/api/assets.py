"""Asset endpoints (Phase 4): poster generation, caption materialization, listing.

Asset bytes are served through `/campaigns/assets/{id}/file`, which resolves
ownership before touching storage — an asset id is not a capability.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, owned_asset, owned_campaign
from ..errors import NotFoundError
from ..models import AssetRecord, User, VerificationResultRecord
from ..services.assets_service import generate_captions, generate_posters, serialize_asset
from ..storage import StorageError, get_storage

router = APIRouter(prefix="/campaigns", tags=["assets"])

#: Media types we are willing to echo back for a stored asset. Anything else is
#: served as an opaque download so a stored file can never be interpreted as
#: active content (HTML/SVG/script) in the browser's origin.
_ALLOWED_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
}


class GenerateRequest(BaseModel):
    variants: int = Field(default=1, ge=1, le=3)
    #: Live mode only: compose on labelled fallback art if the image model
    #: fails. Off by default so a provider failure is reported, not hidden.
    allow_fallback_art: bool = False


@router.post("/{campaign_id}/assets/posters", response_model=list[dict])
def create_posters(
    campaign_id: int,
    payload: GenerateRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    owned_campaign(db, campaign_id, user)
    variants = payload.variants if payload else 1
    records = generate_posters(
        db,
        campaign_id,
        variants=variants,
        allow_fallback_art=bool(payload and payload.allow_fallback_art),
    )
    db.commit()
    return [serialize_asset(r) for r in records]


@router.post("/{campaign_id}/assets/captions", response_model=list[dict])
def create_captions(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    owned_campaign(db, campaign_id, user)
    records = generate_captions(db, campaign_id)
    db.commit()
    return [serialize_asset(r) for r in records]


@router.get("/{campaign_id}/assets")
def list_assets(
    campaign_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> list[dict]:
    owned_campaign(db, campaign_id, user)
    rows = db.scalars(
        select(AssetRecord).where(AssetRecord.campaign_id == campaign_id).order_by(AssetRecord.id)
    ).all()
    result = []
    for r in rows:
        item = serialize_asset(r)
        item["verification"] = [
            {
                "check": v.check_name,
                "verdict": v.verdict,
                "confidence": v.confidence,
                "attempt": v.attempt,
            }
            for v in db.scalars(
                select(VerificationResultRecord)
                .where(VerificationResultRecord.asset_id == r.id)
                .order_by(VerificationResultRecord.id)
            ).all()
        ]
        result.append(item)
    return result


@router.get("/assets/{asset_id}/file")
def asset_file(
    asset_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Response:
    record = owned_asset(db, asset_id, user)
    if not record.storage_path:
        raise NotFoundError(f"Asset {asset_id} has no stored file.")
    try:
        data = get_storage().read_bytes(record.storage_path)
    except StorageError as exc:
        # A row pointing at a missing (or out-of-root) file is a 404, not a 500.
        raise NotFoundError(
            f"Asset {asset_id} has no readable file.", details={"asset_id": asset_id}
        ) from exc
    return Response(
        content=data,
        media_type=_media_type_for(record.storage_path),
        headers={"Content-Disposition": f'inline; filename="asset-{asset_id}{_suffix(record.storage_path)}"'},
    )


def _suffix(storage_path: str) -> str:
    _, _, tail = storage_path.rpartition(".")
    return f".{tail.lower()}" if tail and tail != storage_path else ""


def _media_type_for(storage_path: str) -> str:
    """Resolve a safe media type from the stored path's extension.

    An unrecognized extension is served as an opaque download rather than a
    guessed type, so a stored file can never come back as active content.
    """
    return _ALLOWED_MEDIA_TYPES.get(_suffix(storage_path), "application/octet-stream")
