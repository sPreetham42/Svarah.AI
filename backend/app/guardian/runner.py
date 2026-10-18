"""Guardian runner: verdict aggregation + asset verification entry points (Phase 7)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from ..errors import ValidationError
from ..models import AssetRecord, FactSheetRecord, VerificationResultRecord
from .checks_text import CheckOutcome, verify_text

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_WARN = "WARN"
VERDICT_NEEDS_REVIEW = "NEEDS_REVIEW"

ASSET_STATUS_VERIFIED = "verified"
ASSET_STATUS_FAILED = "failed"
ASSET_STATUS_NEEDS_REVIEW = "needs_review"

#: Name of the persisted aggregate row: the Guardian's authoritative verdict
#: for one asset attempt. Certificates and the publishing gate read THIS row
#: instead of re-deriving a verdict from whichever check row happens to be last.
AGGREGATE_CHECK = "aggregate"

#: Critical checks: their failure forces an overall FAIL regardless of score.
CRITICAL_CHECKS = {"numeric_parity", "token_residue", "unsupported_claims", "fact_parity_days", "registry_vs_tokens", "ocr_cross_check", "asr_roundtrip"}


@dataclass
class Report:
    asset_id: int
    verdict: str
    outcomes: list[CheckOutcome] = field(default_factory=list)
    attempts: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "verdict": self.verdict,
            "attempts": self.attempts,
            "checks": [o.as_dict() for o in self.outcomes],
        }


def aggregate(outcomes: list[CheckOutcome]) -> str:
    failed_critical = any(o.verdict == VERDICT_FAIL and o.check in CRITICAL_CHECKS for o in outcomes)
    if failed_critical:
        return VERDICT_FAIL
    if any(o.verdict == VERDICT_FAIL for o in outcomes):
        return VERDICT_FAIL
    if any(o.verdict == VERDICT_NEEDS_REVIEW for o in outcomes):
        return VERDICT_NEEDS_REVIEW
    if any(o.verdict == VERDICT_WARN for o in outcomes):
        return VERDICT_WARN
    return VERDICT_PASS


def _persist(db: Session, asset_id: int, outcomes: list[CheckOutcome], attempt: int) -> None:
    for outcome in outcomes:
        db.add(
            VerificationResultRecord(
                asset_id=asset_id,
                check_name=outcome.check,
                verdict=outcome.verdict,
                confidence=outcome.confidence,
                method="deterministic",
                details_json=json.dumps(outcome.details, ensure_ascii=False),
                attempt=attempt,
            )
        )
    # The aggregate row is the runner's authoritative, persisted verdict for
    # this attempt; per-check rows remain the evidence trail.
    db.add(
        VerificationResultRecord(
            asset_id=asset_id,
            check_name=AGGREGATE_CHECK,
            verdict=aggregate(outcomes),
            confidence=1.0,
            method="deterministic",
            details_json=json.dumps(
                {"checks": [o.as_dict() for o in outcomes]}, ensure_ascii=False
            ),
            attempt=attempt,
        )
    )


def verify_caption_asset(db: Session, record: AssetRecord, tokens: dict[str, str], *, attempt: int = 1) -> Report:
    conditions = _locked_conditions(db, record)
    outcomes = verify_text(record.text_content or "", tokens, conditions=conditions)
    verdict = aggregate(outcomes)
    _persist(db, record.id, outcomes, attempt)
    record.asset_status = _status_for(verdict)
    db.flush()
    return Report(asset_id=record.id, verdict=verdict, outcomes=outcomes, attempts=attempt)


def _locked_conditions(db: Session, record: AssetRecord) -> list[str]:
    if not record.factsheet_id:
        return []
    sheet = db.get(FactSheetRecord, record.factsheet_id)
    if sheet is None or not sheet.facts_json:
        return []
    payload = json.loads(sheet.facts_json)
    return payload.get("offer", {}).get("conditions") or []


# ── poster: registry vs tokens + optional OCR ─────────────────────────
def verify_poster_asset(db: Session, record: AssetRecord, tokens: dict[str, str], *, attempt: int = 1) -> Report:
    outcomes: list[CheckOutcome] = []
    provenance = json.loads(record.provenance_json or "{}")
    registry = provenance.get("registry", {})
    rendered = registry.get("rendered_facts") or []

    outcomes.append(_check_registry_vs_tokens(rendered, tokens))

    ocr_outcome = _ocr_cross_check(record, rendered)
    outcomes.append(ocr_outcome)

    verdict = aggregate(outcomes)
    _persist(db, record.id, outcomes, attempt)
    record.asset_status = _status_for(verdict)
    db.flush()
    return Report(asset_id=record.id, verdict=verdict, outcomes=outcomes, attempts=attempt)


def _check_registry_vs_tokens(rendered: list[dict[str, Any]], tokens: dict[str, str]) -> CheckOutcome:
    """Every rendered fact string that carries fact classes must come from locked values."""
    violations: list[dict[str, str]] = []
    locked_text_values = set(tokens.values())
    token_number_values = {v for v in tokens.values() if any(c.isdigit() for c in v)}
    for item in rendered:
        text = str(item.get("text") or "")
        key = str(item.get("key") or "")
        if key in ("HEADLINE", "SUBLINE", "FACT"):
            if text not in locked_text_values and not any(v and v in text for v in token_number_values):
                # Headlines combine tokens with framing words; acceptance =
                # each numeric fragment in the string traces to a locked value.
                import re as _re

                numbers = {m.group(0) for m in _re.finditer(r"\d[\d.,]*", text)}
                if not numbers or not all(any(n in v for v in token_number_values) for n in numbers):
                    violations.append({"key": key, "text": text})
    if violations:
        return CheckOutcome("registry_vs_tokens", "FAIL", 1.0, {"violations": violations})
    return CheckOutcome("registry_vs_tokens", "PASS")


def _ocr_cross_check(record: AssetRecord, rendered: list[dict[str, Any]]) -> CheckOutcome:
    """OCR the fact-zone strip when Tesseract is present (Latin scripts only).

    Unavailable/weak OCR yields NEEDS_REVIEW — never a silent pass.
    """
    import shutil

    if shutil.which("tesseract") is None:
        return CheckOutcome(
            "ocr_cross_check",
            VERDICT_NEEDS_REVIEW,
            0.0,
            {"note": "Tesseract not installed; registry remains the authoritative record and OCR limitation is disclosed."},
        )
    try:
        return ocr_crosscheck(record, rendered)
    except Exception as exc:  # noqa: BLE001 - OCR environment problems
        return CheckOutcome(
            "ocr_cross_check",
            VERDICT_NEEDS_REVIEW,
            0.0,
            {"note": f"OCR failed to run: {exc}"},
        )


def ocr_crosscheck(record: AssetRecord, rendered: list[dict[str, Any]]) -> CheckOutcome:
    from ..services.ocr import ocr_fact_zone

    from ..storage import get_storage

    data = get_storage().read_bytes(record.storage_path)
    expected = [str(i.get("text") or "") for i in rendered]
    result = ocr_fact_zone(data, expected=expected)
    return result


# ── voice: ASR round-trip vs locked facts ─────────────────────────────
def verify_voice_asset(db: Session, record: AssetRecord, tokens: dict[str, str], *, attempt: int = 1) -> Report:
    outcomes: list[CheckOutcome] = []
    # 1. Script-level checks (the exact substituted text) — deterministic.
    outcomes.extend(verify_text(record.text_content or "", tokens, conditions=_locked_conditions(db, record)))
    # 2. ASR round-trip on the stored audio (mock provider attributed honestly).
    outcomes.append(asr_roundtrip(record, tokens))
    verdict = aggregate(outcomes)
    _persist(db, record.id, outcomes, attempt)
    record.asset_status = _status_for(verdict)
    db.flush()
    return Report(asset_id=record.id, verdict=verdict, outcomes=outcomes, attempts=attempt)


def asr_roundtrip(record: AssetRecord, tokens: dict[str, str]) -> CheckOutcome:
    from ..services.asr import roundtrip_check

    from ..storage import get_storage

    audio = get_storage().read_bytes(record.storage_path)
    script = record.text_content or ""
    outcome = roundtrip_check(audio, script, tokens, language_code=record.locale)
    return outcome


# ── repair loop (bounded to 2 automatic attempts) ─────────────────────
MAX_REPAIR_ATTEMPTS = 2


def verify_asset(db: Session, record: AssetRecord, *, attempt: int = 1) -> Report:
    sheet_record = db.get(FactSheetRecord, record.factsheet_id) if record.factsheet_id else None
    if sheet_record is None:
        raise ValidationError("Asset has no factsheet binding.", code="asset_unbound")
    tokens = json.loads(sheet_record.tokens_json or "{}")
    if record.kind == "poster":
        report = verify_poster_asset(db, record, tokens, attempt=attempt)
    elif record.kind == "voice":
        report = verify_voice_asset(db, record, tokens, attempt=attempt)
    else:
        report = verify_caption_asset(db, record, tokens, attempt=attempt)
    return report


def repair_asset(db: Session, record: AssetRecord) -> dict[str, Any]:
    """Bounded repair: at most two regenerations, then hand to human review."""
    history: list[dict[str, Any]] = []
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 2):
        report = verify_asset(db, record, attempt=attempt)
        history.append(report.as_dict())
        if report.verdict not in (VERDICT_FAIL,):
            break
        if attempt > MAX_REPAIR_ATTEMPTS:
            record.asset_status = ASSET_STATUS_NEEDS_REVIEW
            db.flush()
            break
        regenerated = _regenerate(db, record)
        if regenerated is None:
            record.asset_status = ASSET_STATUS_NEEDS_REVIEW
            db.flush()
            break
    return {"asset_id": record.id, "history": history, "final_status": record.asset_status}


def _regenerate(db: Session, record: AssetRecord) -> AssetRecord | None:
    """Re-run the deterministic generation for a failed asset when safe."""
    if record.kind == "caption":
        # Captions are deterministic re-substitutions; re-materialize text.
        from ..services.assets_service import generate_captions

        generate_captions(db, record.campaign_id)
        return record
    if record.kind == "poster":
        from ..services.assets_service import generate_posters

        generate_posters(db, record.campaign_id, variants=1)
        return record
    return None  # voice/video regeneration needs provider/asset context


def _status_for(verdict: str) -> str:
    return {
        VERDICT_PASS: ASSET_STATUS_VERIFIED,
        VERDICT_FAIL: ASSET_STATUS_FAILED,
        VERDICT_WARN: ASSET_STATUS_NEEDS_REVIEW,
        VERDICT_NEEDS_REVIEW: ASSET_STATUS_NEEDS_REVIEW,
    }[verdict]


__all__ = [
    "Report",
    "aggregate",
    "verify_asset",
    "repair_asset",
    "verify_caption_asset",
    "verify_poster_asset",
    "verify_voice_asset",
    "MAX_REPAIR_ATTEMPTS",
    "AGGREGATE_CHECK",
    "VERDICT_PASS",
    "VERDICT_FAIL",
    "VERDICT_NEEDS_REVIEW",
]
