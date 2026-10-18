"""Request dependencies: authentication and resource ownership.

Every route that touches owned data resolves its principal here. The rule the
whole API relies on: a resource is addressed by id, but reachability is decided
by the authenticated ``owner_uid`` — never by an identifier the client sent.

Helpers raise rather than return ``None`` so a forgotten check is a 500 in
development instead of a silent data leak.
"""

from __future__ import annotations

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_db
from .errors import AuthenticationError, AuthorizationError, NotFoundError
from .models import AssetRecord, Campaign, FactSheetRecord, PublishRecord, User
from .security import bearer_token, read_token


def current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> User:
    """Resolve the signed bearer token to a live account row.

    The token is self-contained, but the account is still looked up: a deleted
    account must stop working immediately even while its token is unexpired.
    """
    settings: Settings = get_settings()
    token = bearer_token(authorization)
    if token is None:
        raise AuthenticationError(
            "Sign in to continue.", code="auth_required", details={"scheme": "bearer"}
        )
    claims = read_token(token, settings)
    user = db.scalar(select(User).where(User.owner_uid == claims.uid))
    if user is None:
        raise AuthenticationError(
            "This account no longer exists.", code="account_missing"
        )
    return user


def owned_campaign(db: Session, campaign_id: int, user: User) -> Campaign:
    """Load a campaign the caller owns.

    A campaign owned by someone else — and a pre-authentication campaign with
    no owner at all — is reported as *not found* rather than *forbidden*, so
    probing ids cannot be used to discover which campaigns exist.
    """
    campaign = db.get(Campaign, campaign_id)
    if campaign is None or campaign.owner_uid != user.owner_uid:
        raise NotFoundError(
            f"Campaign {campaign_id} was not found.", details={"campaign_id": campaign_id}
        )
    return campaign


def owned_asset(db: Session, asset_id: int, user: User) -> AssetRecord:
    """Load an asset whose campaign the caller owns."""
    asset = db.get(AssetRecord, asset_id)
    if asset is None:
        raise NotFoundError(f"Asset {asset_id} was not found.", details={"asset_id": asset_id})
    owned_campaign(db, asset.campaign_id, user)
    return asset


def owned_factsheet(db: Session, sheet_id: int, user: User) -> FactSheetRecord:
    """Load a fact sheet whose campaign the caller owns."""
    sheet = db.get(FactSheetRecord, sheet_id)
    if sheet is None:
        raise NotFoundError(
            f"FactSheet {sheet_id} was not found.", details={"factsheet_id": sheet_id}
        )
    owned_campaign(db, sheet.campaign_id, user)
    return sheet


def owned_publish_record(db: Session, publish_id: int, user: User) -> PublishRecord:
    """Load a publish record the caller owns.

    Both the record's own ``owner_uid`` and its campaign are checked: the two
    are written together, and a mismatch means the row is not trustworthy for
    an action as consequential as publishing.
    """
    record = db.get(PublishRecord, publish_id)
    if record is None:
        raise NotFoundError(
            f"Publish record {publish_id} was not found.", details={"publish_id": publish_id}
        )
    owned_campaign(db, record.campaign_id, user)
    if record.owner_uid != user.owner_uid:
        raise AuthorizationError(
            "This publication was prepared by another account.",
            details={"publish_id": publish_id},
        )
    return record


__all__ = [
    "current_user",
    "owned_asset",
    "owned_campaign",
    "owned_factsheet",
    "owned_publish_record",
]
