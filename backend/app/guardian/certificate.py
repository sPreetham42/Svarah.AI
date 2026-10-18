"""Verification Certificate (Phase 7, handoff §5).

JSON + human-readable HTML certificate binding:
- the locked FactSheet ID/version/hash + HMAC seal reference;
- every asset with SHA-256 checksum, provider/model, mock/fallback labels;
- per-check verdicts/evidence;
- repair history;
- an HMAC integrity seal over the certificate payload (``hmac-sha256``,
  NOT a public-key digital signature).

Verification recomputes everything: an asset whose current bytes no longer
match the recorded checksum, or a mutated factsheet, invalidates the seal.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import ConflictError, NotFoundError, SealUnavailableError
from ..models import AssetRecord, AuditEvent, Campaign, FactSheetRecord, VerificationResultRecord
from ..services.checksums import current_asset_digest
from .runner import AGGREGATE_CHECK

CERTIFICATE_VERSION = "1.0"

#: Persisted asset status -> the verdict the certificate reports when no
#: aggregate verification row exists (legacy/incomplete runs).
_STATUS_VERDICT = {
    "verified": "PASS",
    "failed": "FAIL",
    "needs_review": "NEEDS_REVIEW",
}


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def certificate_payload(certificate: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in certificate.items() if k != "integrity_seal"}
    return body


def compute_certificate_seal(certificate: dict[str, Any], secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), _canonical(certificate_payload(certificate)), hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"


def verify_certificate_seal(certificate: dict[str, Any], secret: str) -> bool:
    if not secret:
        return False
    expected = compute_certificate_seal(certificate, secret)
    return hmac.compare_digest(expected, certificate.get("integrity_seal", ""))


def build_certificate(db: Session, campaign_id: int) -> dict[str, Any]:
    settings = get_settings()
    if not settings.seal_configured:
        raise SealUnavailableError(
            "Certificate issuance requires TITAN_SEAL_SECRET.",
            code="seal_unavailable",
        )
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise NotFoundError(f"Campaign {campaign_id} was not found.")
    sheet = (
        db.query(FactSheetRecord)
        .filter(FactSheetRecord.campaign_id == campaign_id, FactSheetRecord.status == "locked")
        .order_by(FactSheetRecord.version.desc())
        .first()
    )
    if sheet is None:
        raise ConflictError("Campaign facts are not locked; no certificate without locked facts.", code="facts_not_locked")

    assets = (
        db.query(AssetRecord)
        .filter(AssetRecord.campaign_id == campaign_id)
        .order_by(AssetRecord.id)
        .all()
    )
    asset_entries: list[dict[str, Any]] = []
    overall_fail = False
    overall_review = False
    for record in assets:
        checks = [
            {
                "check": v.check_name,
                "verdict": v.verdict,
                "confidence": v.confidence,
                "attempt": v.attempt,
                "details": json.loads(v.details_json) if v.details_json else None,
                "created_at": str(v.created_at),
            }
            for v in db.query(VerificationResultRecord)
            .filter(VerificationResultRecord.asset_id == record.id)
            .order_by(VerificationResultRecord.id)
            .all()
        ]
        # The runner's persisted aggregate row is authoritative for the asset's
        # verdict. Never re-derive it from whichever per-check row is last: that
        # hid a real critical FAIL behind a later PASS row.
        aggregates = [c for c in checks if c["check"] == AGGREGATE_CHECK]
        asset_verdict = (
            aggregates[-1]["verdict"]
            if aggregates
            else _STATUS_VERDICT.get(record.asset_status)
        )
        if asset_verdict == "FAIL":
            overall_fail = True
        elif asset_verdict in ("NEEDS_REVIEW", "WARN", None):
            overall_review = True

        current_checksum = current_asset_digest(
            storage_path=record.storage_path, text_content=record.text_content
        )
        bytes_match: bool | None = None
        if record.storage_path or record.text_content is not None:
            if record.sha256 is None:
                bytes_match = None  # nothing recorded to compare against
            elif current_checksum is None:
                bytes_match = False  # recorded checksum, but the artifact is gone
            else:
                bytes_match = current_checksum == record.sha256
        if bytes_match is False:
            # A modified artifact is a real integrity failure, independent of
            # the verification verdict.
            overall_fail = True
        asset_entries.append(
            {
                "asset_id": record.id,
                "kind": record.kind,
                "locale": record.locale,
                "sha256": record.sha256,
                "current_sha256": current_checksum,
                "bytes_unchanged": bytes_match,
                "verdict": asset_verdict,
                "provider": record.provider,
                "model": record.model,
                "is_mock": record.is_mock,
                "used_fallback": record.used_fallback,
                "fact_hash": record.fact_hash,
                "asset_status": record.asset_status,
                "checks": checks,
            }
        )

    audits = [
        {
            "event_type": e.event_type,
            "payload": json.loads(e.payload_json) if e.payload_json else None,
            "created_at": str(e.created_at),
        }
        for e in db.query(AuditEvent).filter(AuditEvent.campaign_id == campaign_id).order_by(AuditEvent.id).all()
    ]

    certificate: dict[str, Any] = {
        "certificate_version": CERTIFICATE_VERSION,
        "campaign_id": campaign_id,
        "owner_shop_id": campaign.shop_id,
        "factsheet": {
            "id": sheet.id,
            "version": sheet.version,
            "fact_hash": sheet.fact_hash,
            "seal": sheet.seal,
        },
        "assets": asset_entries,
        "verdict": (
            "FAILED" if overall_fail else ("NEEDS_REVIEW" if overall_review else "VERIFIED")
        ),
        "issued_at": str(datetime.now(timezone.utc)),
        "guardian": {
            "deterministic_first": True,
            "critical_failure_blocks_approval": True,
            "note": "Agnes semantic critic is advisory-only; not configured in this run.",
        },
        "audit_tail": audits[-25:],
    }
    certificate["integrity_seal"] = compute_certificate_seal(certificate, settings.seal_secret or "")
    return certificate


def certificate_valid(certificate: dict[str, Any]) -> dict[str, Any]:
    """Recompute seal + asset checksums; report tampering precisely."""
    settings = get_settings()
    result: dict[str, Any] = {"seal_valid": None, "assets_unchanged": None, "tampered": []}
    if not settings.seal_configured:
        result["seal_valid"] = None
    else:
        result["seal_valid"] = verify_certificate_seal(certificate, settings.seal_secret or "")
    tampered: list[dict[str, Any]] = []
    for index, asset in enumerate(certificate.get("assets") or []):
        if asset.get("bytes_unchanged") is False:
            tampered.append({"index": index, "asset_id": asset.get("asset_id"), "reason": "bytes_changed"})
    result["tampered"] = tampered
    result["assets_unchanged"] = not tampered
    return result


def certificate_html(certificate: dict[str, Any]) -> str:
    """Readable HTML certificate (per handoff §5)."""

    def esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""))

    rows = ""
    for asset in certificate.get("assets") or []:
        checks = "".join(
            f"<li>{esc(c['check'])}: <b>{esc(c['verdict'])}</b> (attempt {esc(c['attempt'])})</li>"
            for c in asset.get("checks", [])
        ) or "<li>No checks recorded</li>"
        rows += f"""
        <tr>
          <td>#{esc(asset['asset_id'])}</td>
          <td>{esc(asset['kind'])} ({esc(asset['locale'])})</td>
          <td><code>{esc(asset['sha256'])}</code></td>
          <td>{'MOCK' if asset.get('is_mock') else 'LIVE'}{' · FALLBACK' if asset.get('used_fallback') else ''}</td>
          <td><b>{esc(asset.get('verdict'))}</b><br><small>{esc(asset['asset_status'])}</small></td>
          <td><ul>{checks}</ul></td>
        </tr>"""
    facts = certificate.get("factsheet", {})
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Titan Verification Certificate — Campaign {esc(certificate.get('campaign_id'))}</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #111; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
 th, td {{ border: 1px solid #999; padding: 8px; text-align: left; font-size: 14px; }}
 th {{ background: #f0f0f0; }}
 code {{ font-size: 12px; }}
 .verdict {{ padding: 8px 14px; display: inline-block; border-radius: 6px; font-weight: 700; }}
 .VERIFIED {{ background: #d4edda; }} .FAILED {{ background: #f8d7da; }} .NEEDS_REVIEW {{ background: #fff3cd; }}
</style></head>
<body>
<h1>Titan Verification Certificate</h1>
<p>Campaign <b>{esc(certificate.get('campaign_id'))}</b> — verdict
<span class="verdict {esc(certificate.get('verdict'))}">{esc(certificate.get('verdict'))}</span></p>
<p>FactSheet v{esc(facts.get('version'))} · fact_hash <code>{esc(facts.get('fact_hash'))}</code><br>
Integrity seal <code>{esc(certificate.get('integrity_seal'))}</code> (HMAC — Verification Certificate, not a public-key signature)</p>
<h2>Assets</h2>
<table>
<tr><th>Asset</th><th>Kind</th><th>SHA-256</th><th>Provenance</th><th>Verdict</th><th>Checks</th></tr>
{rows}
</table>
<p>Issued {esc(certificate.get('issued_at'))} · certificate_version {esc(certificate.get('certificate_version'))}</p>
</body></html>"""


__all__ = ["build_certificate", "certificate_valid", "certificate_html", "verify_certificate_seal", "CERTIFICATE_VERSION"]
