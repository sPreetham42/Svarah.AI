"""Post a finished campaign to the owner's own social accounts.

The same server-side gate as the studio publisher applies: only a poster and a
caption the Guardian verified (or the owner accepted) against the *current*
locked facts can leave Svarah. Every attempt is stored as a ``PublishRecord``
and nothing is reported as posted unless the provider said so.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError
from ..models import AssetRecord, AuditEvent, Campaign, PublishRecord, User
from ..storage import get_storage
from .publisher_service import PUBLISHABLE_ASSET_STATUSES, _caption_source, _current_digest, _ensure_verified
from .uploadpost import PLATFORMS, get_social_client
from .windsor import caption_hash

ACTION_ID = "upload_post.upload_photos"


def profile_name(user: User) -> str:
    """The owner's stable profile id at the posting service."""
    return "svarah-" + re.sub(r"[^A-Za-z0-9_-]", "-", user.owner_uid)[:48]


def status(user: User) -> dict[str, Any]:
    """Whether posting is available and which of the owner's accounts are linked."""
    settings = get_settings()
    configured = settings.is_mock or settings.uploadpost_configured
    linked: dict[str, dict[str, Any]] = {}
    if configured:
        linked = get_social_client(settings).accounts(profile_name(user))
    return {
        "provider": "upload-post",
        "configured": configured,
        "is_mock": settings.is_mock,
        "accounts": [
            {
                "platform": platform,
                "connected": platform in linked and not linked[platform].get("reauth_required"),
                "handle": linked.get(platform, {}).get("handle") or None,
                "reauth_required": bool(linked.get(platform, {}).get("reauth_required")),
            }
            for platform in PLATFORMS
        ],
    }


def connect_link(user: User, redirect_url: str | None) -> dict[str, Any]:
    """A link to the hosted page where the owner signs in to their accounts."""
    client = get_social_client()
    username = profile_name(user)
    client.ensure_profile(username)
    return {"access_url": client.connect_url(username, redirect_url=redirect_url, platforms=PLATFORMS), "is_mock": client.is_mock}


def _poster(db: Session, campaign_id: int) -> AssetRecord:
    posters = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id, AssetRecord.kind == "poster")
        .order_by(AssetRecord.id.desc())
        .all()
    )
    if not posters:
        raise ConflictError("This campaign has no poster to post yet.", code="asset_missing")
    for poster in posters:
        if poster.asset_status in PUBLISHABLE_ASSET_STATUSES:
            return poster
    return posters[0]  # the gate below explains why it cannot be posted


def _caption(db: Session, poster: AssetRecord) -> AssetRecord:
    """The Instagram caption when there is one, else the publisher's default."""
    for caption in (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == poster.campaign_id, AssetRecord.kind == "caption", AssetRecord.locale == "en-IN")
        .order_by(AssetRecord.id)
        .all()
    ):
        channel = (json.loads(caption.provenance_json or "{}") or {}).get("channel")
        if channel == "instagram" and (caption.text_content or "").strip():
            return caption
    return _caption_source(db, poster)


def _serialize(record: PublishRecord) -> dict[str, Any]:
    ids = json.loads(record.provider_result_ids_json or "{}")
    error = json.loads(record.error_json or "{}")
    return {
        "publish_id": record.id,
        "platform": record.destination,
        "account": record.account_id,
        "status": record.status,
        "url": ids.get("url"),
        "post_id": ids.get("post_id"),
        "error": error.get("error"),
    }


def post_campaign(db: Session, campaign: Campaign, user: User, platforms: list[str] | None = None) -> dict[str, Any]:
    """Post the campaign's poster + caption to the owner's linked accounts."""
    client = get_social_client()
    poster = _poster(db, campaign.id)
    _ensure_verified(db, poster)
    caption_asset = _caption(db, poster)
    if caption_asset.id != poster.id:
        _ensure_verified(db, caption_asset)
    caption = (caption_asset.text_content or "").strip()

    username = profile_name(user)
    linked = {p: a for p, a in client.accounts(username).items() if p in PLATFORMS and not a.get("reauth_required")}
    wanted = [p for p in (platforms or PLATFORMS) if p in linked]
    if not wanted:
        raise ConflictError(
            "None of your social accounts are connected yet.", code="social_not_connected"
        )

    # A platform this poster already went out on is never posted twice.
    earlier = {
        r.destination: r
        for r in db.query(PublishRecord).filter(
            PublishRecord.campaign_id == campaign.id,
            PublishRecord.asset_id == poster.id,
            PublishRecord.action_id == ACTION_ID,
            PublishRecord.status == "PUBLISHED",
        )
    }
    results = [dict(_serialize(earlier[p]), already_posted=True) for p in wanted if p in earlier]
    todo = [p for p in wanted if p not in earlier]
    if todo:
        data = get_storage().read_bytes(poster.storage_path)
        body = client.upload_photo(
            username,
            platforms=todo,
            caption=caption,
            filename=f"svarah-poster-{poster.id}.png",
            data=data,
            mime="image/png",
            idempotency_key=f"svarah-{campaign.id}-{poster.id}-{'-'.join(todo)}",
        )
        outcomes = body.get("results") if isinstance(body.get("results"), dict) else {}
        for platform in todo:
            outcome = outcomes.get(platform) if isinstance(outcomes.get(platform), dict) else {}
            if outcome.get("success"):
                state, error = "PUBLISHED", None
            elif not outcomes and body.get("request_id"):
                state, error = "PUBLISHING", None  # the provider is still uploading in the background
            else:
                state = "FAILED"
                error = outcome.get("error") or outcome.get("message") or (
                    "This account is not connected." if outcome.get("skipped") else "The network did not confirm the post."
                )
            record = PublishRecord(
                campaign_id=campaign.id,
                asset_id=poster.id,
                owner_uid=user.owner_uid,
                destination=platform,
                account_id=(linked[platform].get("handle") or "")[:128] or None,
                caption_hash=caption_hash(caption),
                asset_hashes_json=json.dumps({str(poster.id): _current_digest(poster), str(caption_asset.id): _current_digest(caption_asset)}),
                action_id=ACTION_ID,
                status=state,
                response_json=json.dumps(outcome or {"request_id": body.get("request_id")}, ensure_ascii=False),
                provider_result_ids_json=json.dumps(
                    {"url": outcome.get("url") or outcome.get("post_url"), "post_id": outcome.get("post_id") or outcome.get("id"), "request_id": body.get("request_id")}
                ),
                error_json=json.dumps({"error": str(error)[:500]}) if error else None,
            )
            db.add(record)
            db.flush()
            results.append(_serialize(record))
        db.add(
            AuditEvent(
                campaign_id=campaign.id,
                event_type="publish.social",
                payload_json=json.dumps(
                    {"asset_id": poster.id, "is_mock": client.is_mock, "results": {r["platform"]: r["status"] for r in results}}
                ),
            )
        )
    return {"is_mock": client.is_mock, "asset_id": poster.id, "caption": caption, "results": results}


__all__ = ["profile_name", "status", "connect_link", "post_campaign"]
