"""Publisher service (Phase 8): approval-bound publication + manual/export fallback."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, ValidationError
from ..models import AssetRecord, AuditEvent, Campaign, PublishRecord
from ..services.campaign_brain import get_locked_sheet
from ..services.checksums import current_asset_digest
from ..services.windsor import (
    PublishPreparation,
    caption_hash,
    execute_publication,
    get_mcp_client,
    prepare_publication,
    wa_me_url,
)


def _audit(db: Session, campaign_id: int, event_type: str, payload: dict[str, Any] | None = None) -> None:
    db.add(AuditEvent(campaign_id=campaign_id, event_type=event_type, payload_json=json.dumps(payload) if payload else None))


def list_actions(db: Session, campaign_id: int) -> dict[str, Any]:
    settings = get_settings()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    client = get_mcp_client(settings)
    connectors = client.call_tool("get_connectors")
    actions_raw = client.call_tool("list_actions")
    from ..services.windsor import DiscoveredAction

    actions = [
        DiscoveredAction(
            action_id=item.get("id", ""),
            platform=_platform_from_action(item.get("id", "")),
            media_kind=_media_kind_from_action(item, connectors),
            description=item.get("description", ""),
            schema=item.get("inputSchema") or {},
        )
        for item in actions_raw.get("actions", [])
        if isinstance(item, dict)
    ]
    return {
        "is_mock": not settings.windsor_configured or settings.is_mock,
        "connectors": connectors.get("connectors", []),
        "actions": [a.as_dict() for a in actions],
    }


def _platform_from_action(action_id: str) -> str:
    lowered = action_id.lower()
    for platform in ("instagram", "facebook", "x", "whatsapp"):
        if platform in lowered:
            return platform
    return lowered


def _media_kind_from_action(item: dict[str, Any], connectors: dict[str, Any]) -> str:
    text = json.dumps(item).lower()
    if "video" in text or "reel" in text:
        return "video"
    if "image" in text or "photo" in text:
        return "image"
    return "text"


#: Only these asset statuses may be prepared, approved or published. Everything
#: else (pending / validating / needs_review / failed) is unresolved or failed.
PUBLISHABLE_ASSET_STATUSES = frozenset({"verified", "human_verified"})


def _current_digest(asset: AssetRecord | None) -> str | None:
    if asset is None:
        return None
    return current_asset_digest(storage_path=asset.storage_path, text_content=asset.text_content)


def _ensure_verified(db: Session, asset: AssetRecord) -> None:
    """Server-side publishing gate — frontend restrictions are NOT trusted.

    Rejects an asset that is failed/unresolved, or that was generated against
    superseded facts (stale). Called at preparation, approval and execution, so
    a status change between steps cannot slip through.
    """
    if asset.asset_status not in PUBLISHABLE_ASSET_STATUSES:
        raise ConflictError(
            f"Asset {asset.id} has status {asset.asset_status!r}; only verified assets "
            "can be published. Run the Guardian verification first.",
            code="asset_not_verified",
            details={"asset_id": asset.id, "asset_status": asset.asset_status},
        )
    sheet = get_locked_sheet(db, asset.campaign_id)
    if (asset.fact_hash or "") != (sheet.fact_hash or ""):
        raise ConflictError(
            f"Asset {asset.id} was generated against superseded facts; "
            "regenerate and re-verify it before publishing.",
            code="asset_stale",
            details={
                "asset_id": asset.id,
                "asset_fact_hash": asset.fact_hash,
                "locked_fact_hash": sheet.fact_hash,
            },
        )


def _caption_source(db: Session, asset: AssetRecord) -> AssetRecord:
    """The asset that supplies the published caption.

    Text assets carry their own caption. Posters carry none, so the campaign's
    first en-IN caption is pulled (deterministic pick) and is itself gated.
    """
    if (asset.text_content or "").strip():
        return asset
    caption_asset = (
        db.query(AssetRecord)
        .filter(
            AssetRecord.campaign_id == asset.campaign_id,
            AssetRecord.kind == "caption",
            AssetRecord.locale.in_(("en-IN",)),
        )
        .order_by(AssetRecord.id)
        .first()
    )
    if caption_asset is None or not (caption_asset.text_content or "").strip():
        raise ValidationError(
            "No caption available for publication; materialize captions first.",
            code="caption_missing",
        )
    return caption_asset


def _ensure_approval_binding(db: Session, record: PublishRecord) -> str:
    """Re-validate the approved binding: still verified, unchanged since approval.

    Returns the caption text the publication binds. Raises ``approval_stale``
    when the caption or any bound artifact changed after approval.
    """
    asset = db.get(AssetRecord, record.asset_id) if record.asset_id else None
    if asset is None:
        raise ConflictError("Publish record is not bound to an asset.", code="asset_missing")
    _ensure_verified(db, asset)
    caption_asset = _caption_source(db, asset)
    if caption_asset.id != asset.id:
        _ensure_verified(db, caption_asset)
    caption = caption_asset.text_content or ""
    if record.caption_hash and caption_hash(caption) != record.caption_hash:
        raise ConflictError(
            "The caption changed after approval; re-prepare and re-approve before publishing.",
            code="approval_stale",
            details={"publish_id": record.id},
        )
    recorded = json.loads(record.asset_hashes_json or "{}")
    for asset_id_str, expected in recorded.items():
        if not expected:
            continue  # legacy record with no digest recorded for this asset
        try:
            bound = db.get(AssetRecord, int(asset_id_str))
        except (TypeError, ValueError):
            continue
        if bound is None:
            raise ConflictError(
                "A bound asset no longer exists; re-prepare before publishing.",
                code="asset_missing",
                details={"asset_id": asset_id_str, "publish_id": record.id},
            )
        current = _current_digest(bound)
        if current != expected:
            raise ConflictError(
                "A bound asset changed after approval; re-verify and re-approve before publishing.",
                code="approval_stale",
                details={
                    "publish_id": record.id,
                    "asset_id": bound.id,
                    "expected_sha256": expected,
                    "current_sha256": current,
                },
            )
    return caption


def prepare(
    db: Session,
    *,
    campaign_id: int,
    asset_id: int,
    platform: str,
    media_kind: str,
    owner_uid: str,
) -> dict[str, Any]:
    """Bind ONE verified asset + caption to a destination for review."""
    asset = db.get(AssetRecord, asset_id)
    if asset is None or asset.campaign_id != campaign_id:
        raise NotFoundError(f"Asset {asset_id} was not found on campaign {campaign_id}.")
    compatible = (
        (asset.kind == media_kind)
        or (asset.kind == "poster" and media_kind == "image")
        or (asset.kind == "caption" and media_kind == "text")
        or (asset.kind == "voice" and media_kind == "text")
    )
    if not compatible:
        raise ValidationError(
            f"Asset kind {asset.kind!r} does not match requested {media_kind!r}.",
            code="asset_kind_mismatch",
        )
    # Gate BEFORE creating any publish record: failed/unverified/stale assets
    # are rejected here, again at approval, and again at execution.
    _ensure_verified(db, asset)
    caption_asset = _caption_source(db, asset)
    if caption_asset.id != asset.id:
        _ensure_verified(db, caption_asset)
    caption = caption_asset.text_content or ""

    disclosure = list_actions(db, campaign_id)
    actions = [
        __import__("app.services.windsor", fromlist=["DiscoveredAction"]).DiscoveredAction(**a)
        for a in disclosure["actions"]
    ]

    asset_url: str | None = None
    if asset.storage_path:
        asset_url = f"/api/campaigns/assets/{asset.id}/file"  # relative fetch URL

    # Sandbox destination exercises the mock registry (no real social call).
    effective_platform = "instagram" if platform == "sandbox" else platform
    preparation = prepare_publication(
        platform=effective_platform, media_kind=media_kind, caption=caption, asset_url=asset_url, actions=actions,
    )

    bound_digests = {
        str(asset.id): _current_digest(asset),
        str(caption_asset.id): _current_digest(caption_asset),
    }
    record = PublishRecord(
        campaign_id=campaign_id,
        asset_id=asset.id,
        owner_uid=owner_uid,
        destination=platform,
        caption_hash=caption_hash(caption) if caption else None,
        asset_hashes_json=json.dumps(bound_digests),
        action_id=preparation.action.action_id if preparation.action else None,
        action_schema_json=json.dumps(preparation.action.schema) if preparation.action else None,
        status=preparation.status,
        payload_json=json.dumps(preparation.payload) if preparation.payload else None,
        response_json=json.dumps(preparation.manual_package) if preparation.manual_package else None,
    )
    db.add(record)
    db.flush()
    _audit(db, campaign_id, "publish.prepared", {"publish_id": record.id, "destination": platform, "status": record.status})
    return _serialize(record)


def approve(db: Session, publish_id: int) -> dict[str, Any]:
    record = db.get(PublishRecord, publish_id)
    if record is None:
        raise NotFoundError(f"Publish record {publish_id} was not found.")
    if record.status not in ("READY_FOR_REVIEW",):
        raise ConflictError(
            f"Cannot approve in status {record.status!r}.", code="approval_invalid"
        )
    # Approval re-checks the gate: a verification/status change between prepare
    # and approve must block approval.
    _ensure_approval_binding(db, record)
    record.status = "APPROVED"
    db.flush()
    _audit(db, record.campaign_id, "publish.approved", {"publish_id": record.id, "caption_hash": record.caption_hash})
    return _serialize(record)


def execute(db: Session, publish_id: int) -> dict[str, Any]:
    """Execute an APPROVED publication; idempotent by status guard."""
    settings = get_settings()
    record = db.get(PublishRecord, publish_id)
    if record is None:
        raise NotFoundError(f"Publish record {publish_id} was not found.")
    if record.status == "PUBLISHED":
        return _serialize(record)  # idempotent no-op
    if record.status != "APPROVED":
        raise ConflictError("Publish record requires explicit approval before execution.", code="approval_required")
    # Execution re-validates verification state and the approval binding; an
    # asset edited (or verification re-run to FAIL) after approval cannot publish.
    caption = _ensure_approval_binding(db, record)
    if record.destination == "whatsapp":
        record.response_json = json.dumps({"wa_me_url": wa_me_url(caption)})
        record.status = "PUBLISHED"
        db.flush()
        _audit(db, record.campaign_id, "publish.wa_me_prepared", {"publish_id": record.id})
        return _serialize(record)
    if settings.is_mock and record.destination not in ("sandbox",):
        raise ConflictError(
            "Mock mode never calls social endpoints; use destination='sandbox'.",
            code="mock_publish_blocked",
        )

    from ..services.windsor import DiscoveredAction

    action = DiscoveredAction(
        action_id=record.action_id or "",
        platform=record.destination,
        media_kind="image",
        schema=json.loads(record.action_schema_json or "{}"),
    )
    record.status = "PUBLISHING"
    db.flush()
    preparation = PublishPreparation(
        platform=record.destination,
        media_kind="image",
        action=action,
        status="READY_FOR_REVIEW",
        payload=json.loads(record.payload_json or "{}"),
    )
    client = get_mcp_client(settings)
    response = execute_publication(preparation, approval_id=record.id, client=client)
    if response.get("isError"):
        record.status = "FAILED"
        record.error_json = json.dumps(response, ensure_ascii=False)
    else:
        record.status = "PUBLISHED"
        record.response_json = json.dumps(response, ensure_ascii=False)
        result = response.get("result") or {}
        record.provider_result_ids_json = json.dumps({"post_id": result.get("post_id")})
    db.flush()
    _audit(
        db, record.campaign_id, "publish.executed",
        {"publish_id": record.id, "status": record.status, "is_mock": not settings.windsor_configured},
    )
    return _serialize(record)


def _serialize(record: PublishRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "campaign_id": record.campaign_id,
        "asset_id": record.asset_id,
        "destination": record.destination,
        "status": record.status,
        "action_id": record.action_id,
        "caption_hash": record.caption_hash,
        "asset_hashes": json.loads(record.asset_hashes_json or "{}"),
        "payload": json.loads(record.payload_json) if record.payload_json else None,
        "response": json.loads(record.response_json) if record.response_json else None,
        "provider_result_ids": json.loads(record.provider_result_ids_json) if record.provider_result_ids_json else None,
        "error": json.loads(record.error_json) if record.error_json else None,
    }


def list_records(db: Session, campaign_id: int) -> list[dict[str, Any]]:
    """Every publication attempt for a campaign, oldest first."""
    rows = (
        db.query(PublishRecord)
        .filter(PublishRecord.campaign_id == campaign_id)
        .order_by(PublishRecord.id)
        .all()
    )
    return [_serialize(r) for r in rows]


def export_package(db: Session, campaign_id: int) -> dict[str, Any]:
    """Manual-ready export bundle (captions + publish manifest + asset refs)."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    assets = db.query(AssetRecord).filter(AssetRecord.campaign_id == campaign_id).all()
    captions = {a.locale + "/" + (a.text_content or "")[:40]: a.text_content for a in assets if a.kind == "caption"}
    manifest = {
        "campaign_id": campaign_id,
        "generated_notice": "Manual publishing package — Titan never fakes platform success.",
        "captions": captions,
        "assets": [
            {
                "id": a.id,
                "kind": a.kind,
                "path": a.storage_path,
                "sha256": a.sha256,
                "is_mock": a.is_mock,
            }
            for a in assets
        ],
        "publish_records": [
            {
                "destination": r.destination,
                "status": r.status,
                "caption_hash": r.caption_hash,
            }
            for r in db.query(PublishRecord).filter(PublishRecord.campaign_id == campaign_id).all()
        ],
    }
    return manifest


__all__ = ["list_actions", "prepare", "approve", "execute", "export_package", "list_records"]
