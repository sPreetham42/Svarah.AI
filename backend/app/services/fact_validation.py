"""FactSheet validation: deterministic rules first, Agnes semantics second.

Two layers, deliberately kept apart:

1. **Deterministic** — the application schema (`FactSheet`), required-field
   rules and cross-field invariants. This layer always runs, is the only one
   that can block a lock, and never depends on a model.
2. **Semantic (Agnes)** — the model compares the facts with the transcript and
   reports contradictions, unsupported claims, malformed offers and
   inconsistencies. Its output is advisory, is itself schema-validated before
   it is stored, and a failed call is reported as "unavailable" rather than as
   a clean bill of health.

Findings carry a dotted field path (`offer.discount_percent`) so the review
screen can point at the exact input. Results are persisted beside the
extraction status on the fact sheet, with a digest of the facts they were
computed for so the UI can tell when they have gone stale.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from ..config import Settings, get_settings
from ..errors import NormalizationError, ProviderUnavailableError, TitanError
from ..models import FactSheetRecord, utcnow
from .agnes import get_llm_client, parse_llm_json
from .fact_engine import (
    canonical_json,
    canonical_payload,
    lock_blockers,
    normalize_fact_data,
)
from .fact_schemas import FactSheet

#: Every path a finding may point at. Anything else is filed under "general".
FIELD_PATHS: tuple[str, ...] = (
    "business.name",
    "business.location",
    "offer.product",
    "offer.discount_percent",
    "offer.discount_flat",
    "offer.price",
    "offer.quantity",
    "offer.audience",
    "offer.days",
    "offer.date_start",
    "offer.date_end",
    "offer.start_time",
    "offer.end_time",
    "offer.conditions",
    "offer.location",
    "languages",
)
GENERAL_FIELD = "general"

SEVERITIES = ("error", "warning", "info")
MAX_SEMANTIC_FINDINGS = 20
#: A malformed model answer is retried this many times, then reported.
SEMANTIC_MAX_ATTEMPTS = 2


def finding(
    field: str,
    severity: str,
    code: str,
    message: str,
    *,
    source: str,
    suggestion: str | None = None,
) -> dict[str, Any]:
    return {
        "field": field if field in FIELD_PATHS else GENERAL_FIELD,
        "severity": severity,
        "code": code,
        "message": message,
        "suggestion": suggestion,
        "source": source,
    }


def facts_digest(sheet: FactSheet) -> str:
    """Stable digest of the authoritative part of a sheet."""
    canonical = canonical_json(canonical_payload(sheet))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── layer 1: deterministic ─────────────────────────────────────────────
_REQUIRED_HINTS = {
    "offer.product": "Add the product or service on offer.",
}


def deterministic_findings(sheet: FactSheet) -> list[dict[str, Any]]:
    """Schema-level and cross-field checks. No model, no network."""
    out: list[dict[str, Any]] = []
    offer = sheet.offer

    for path in lock_blockers(sheet):
        out.append(
            finding(
                path,
                "error",
                "missing_required",
                "This detail is required before the facts can be locked.",
                source="schema",
                suggestion=_REQUIRED_HINTS.get(path, "Fill it in, or correct the related fields."),
            )
        )

    if offer.date_start and offer.date_end and offer.date_end < offer.date_start:
        out.append(
            finding(
                "offer.date_end",
                "error",
                "date_range_inverted",
                f"The end date ({offer.date_end}) is before the start date ({offer.date_start}).",
                source="schema",
                suggestion="Swap or correct the dates.",
            )
        )

    if offer.start_time and offer.end_time and offer.end_time <= offer.start_time:
        out.append(
            finding(
                "offer.end_time",
                "warning",
                "time_window_inverted",
                f"The closing time ({offer.end_time}) is not after the opening time "
                f"({offer.start_time}).",
                source="schema",
                suggestion="Correct the times unless the offer really runs past midnight.",
            )
        )
    elif bool(offer.start_time) != bool(offer.end_time):
        missing_side = "offer.end_time" if offer.start_time else "offer.start_time"
        out.append(
            finding(
                missing_side,
                "warning",
                "time_window_incomplete",
                "Only one end of the time window is set, so no time window will be printed.",
                source="schema",
                suggestion="Add the other time, or clear both.",
            )
        )

    if offer.discount_percent is not None and offer.discount_flat is not None:
        out.append(
            finding(
                "offer.discount_flat",
                "warning",
                "conflicting_discounts",
                "Both a percentage and a flat discount are set; only the percentage is used in copy.",
                source="schema",
                suggestion="Keep the one you actually offer.",
            )
        )
    if offer.discount_percent is not None and offer.discount_percent in (0, 100):
        out.append(
            finding(
                "offer.discount_percent",
                "warning",
                "discount_extreme",
                f"A {int(offer.discount_percent)}% discount is unusual.",
                source="schema",
                suggestion="Confirm the percentage.",
            )
        )
    if (
        offer.discount_flat is not None
        and offer.price is not None
        and offer.discount_flat > offer.price
    ):
        out.append(
            finding(
                "offer.discount_flat",
                "error",
                "discount_exceeds_price",
                "The flat discount is larger than the price.",
                source="schema",
                suggestion="Correct the discount or the price.",
            )
        )

    for item in sheet.ambiguities:
        candidates = f" ({' / '.join(item.candidates)})" if item.candidates else ""
        out.append(
            finding(
                item.field,
                "warning",
                "ambiguous",
                f"{item.note}{candidates}",
                source="schema",
                suggestion="Choose the correct value.",
            )
        )
    return out


def schema_findings(draft: Any) -> tuple[FactSheet | None, list[dict[str, Any]]]:
    """Validate raw stored JSON against the canonical schema.

    Returns the parsed sheet, or `None` plus field-level schema errors.
    """
    try:
        return normalize_fact_data(draft if isinstance(draft, dict) else {}), []
    except NormalizationError as exc:
        findings: list[dict[str, Any]] = []
        details = exc.details if isinstance(exc.details, list) else []
        for error in details:
            location = ".".join(str(part) for part in error.get("loc", ()) if isinstance(part, str))
            findings.append(
                finding(
                    location,
                    "error",
                    "schema_invalid",
                    str(error.get("msg") or "Invalid value."),
                    source="schema",
                )
            )
        if not findings:
            findings.append(
                finding(GENERAL_FIELD, "error", "schema_invalid", exc.message, source="schema")
            )
        return None, findings


# ── layer 2: semantic (Agnes) ──────────────────────────────────────────
class _SemanticFinding(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str = Field(default=GENERAL_FIELD, max_length=120)
    severity: Literal["error", "warning", "info"] = "warning"
    issue: str = Field(default="other", max_length=60)
    explanation: str = Field(min_length=1, max_length=600)
    suggestion: str | None = Field(default=None, max_length=400)


class _SemanticReport(BaseModel):
    model_config = ConfigDict(extra="ignore")

    findings: list[_SemanticFinding] = Field(default_factory=list)
    summary: str | None = Field(default=None, max_length=600)


_SEMANTIC_SYSTEM = """You audit structured facts extracted from a shop owner's own words.

Compare FACTS with TRANSCRIPT and report only real problems:
- contradiction: a fact disagrees with the transcript or with another fact.
- unsupported_claim: a fact value that the transcript does not state.
- malformed_offer: an offer that cannot work as written (e.g. impossible
  discount, end before start, price and discount that do not fit together).
- inconsistency: values that are individually valid but do not fit together.
- missing_information: something the transcript clearly states that FACTS lack.

Rules:
- Never invent facts and never propose a value the transcript does not contain.
- An empty/null fact is NOT a problem unless the transcript states that value.
- If there is no transcript, only check FACTS against each other.
- business.name comes from the owner's account profile, so it does not have to
  appear in the transcript. Report it only if the transcript names a DIFFERENT
  business.
- "field" must be one of VALID_FIELDS, or "general".
- Return ONLY a JSON object, no prose, no code fences:

{"findings": [{"field": "...", "severity": "error|warning|info",
               "issue": "contradiction|unsupported_claim|malformed_offer|inconsistency|missing_information|other",
               "explanation": "one or two sentences, in English",
               "suggestion": "what the owner should do, or null"}],
 "summary": "one sentence"}

Return {"findings": [], "summary": "..."} when the facts are sound."""


def build_semantic_prompt(sheet: FactSheet, transcript: str | None) -> tuple[str, str]:
    envelope = {
        "TRANSCRIPT": transcript or None,
        "FACTS": canonical_payload(sheet),
        "VALID_FIELDS": list(FIELD_PATHS),
    }
    return _SEMANTIC_SYSTEM, json.dumps(envelope, ensure_ascii=False)


def _mock_semantic(sheet: FactSheet, transcript: str | None) -> dict[str, Any]:
    """Offline stand-in: a literal evidence check, labelled as mock.

    Flags listed values (product, audience) and numbers that do not literally
    appear in the transcript. It makes no semantic judgement.
    """
    findings: list[dict[str, Any]] = []
    if transcript:
        lowered = transcript.lower()
        digits = set(re.findall(r"\d+(?:\.\d+)?", transcript.replace(",", "")))
        for path, values in (
            ("offer.product", sheet.offer.product),
            ("offer.audience", sheet.offer.audience),
        ):
            for value in values:
                if value.lower() not in lowered:
                    findings.append(
                        finding(
                            path,
                            "warning",
                            "unsupported_claim",
                            f"“{value}” does not appear in the transcript.",
                            source="mock",
                            suggestion="Confirm it is correct, or remove it.",
                        )
                    )
        for path, number in (
            ("offer.discount_percent", sheet.offer.discount_percent),
            ("offer.discount_flat", sheet.offer.discount_flat),
            ("offer.price", sheet.offer.price),
            ("offer.quantity", sheet.offer.quantity),
        ):
            if number is None:
                continue
            text = str(int(number)) if float(number).is_integer() else str(number)
            if text not in digits:
                findings.append(
                    finding(
                        path,
                        "info",
                        "unsupported_claim",
                        f"The number {text} is not written as digits in the transcript.",
                        source="mock",
                        suggestion="Confirm the value.",
                    )
                )
    return {
        "status": "ok",
        "provider": "mock",
        "model": "literal-evidence-check",
        "is_mock": True,
        "message": "Offline check: literal evidence only — no semantic model was used.",
        "summary": None,
        "findings": findings,
    }


async def semantic_findings(
    sheet: FactSheet, transcript: str | None, settings: Settings
) -> dict[str, Any]:
    """Agnes semantic validation. Never raises: failures are a reported status."""
    if settings.is_mock:
        return _mock_semantic(sheet, transcript)

    try:
        client = get_llm_client(settings)
    except ProviderUnavailableError as exc:
        return _semantic_unavailable(exc.message, code=exc.code)

    system, user = build_semantic_prompt(sheet, transcript)
    last_problem = "Agnes returned an unusable validation report."
    for attempt in range(SEMANTIC_MAX_ATTEMPTS):
        prompt = user
        if attempt:
            prompt = json.dumps(
                {
                    "previous_output_rejected": last_problem,
                    "instruction": "Return ONLY the JSON object described by the system message.",
                    "request": json.loads(user),
                },
                ensure_ascii=False,
            )
        try:
            result = await client.complete_json(system, prompt, max_tokens=1200, temperature=0.1)
            report = _SemanticReport.model_validate(parse_llm_json(result))
        except ProviderUnavailableError as exc:
            if exc.code in ("llm_bad_output", "llm_bad_json"):
                last_problem = exc.message
                continue
            # Auth, quota, rate limit, network: retrying the prompt cannot help.
            return _semantic_unavailable(exc.message, code=exc.code)
        except PydanticValidationError as exc:
            last_problem = f"The report did not match the schema ({exc.error_count()} error(s))."
            continue
        except TitanError as exc:
            return _semantic_unavailable(exc.message, code=exc.code)

        findings = [
            finding(
                item.field.strip(),
                item.severity,
                re.sub(r"[^a-z_]", "", item.issue.strip().lower().replace(" ", "_")) or "other",
                item.explanation.strip(),
                source="agnes",
                suggestion=(item.suggestion or "").strip() or None,
            )
            for item in report.findings[:MAX_SEMANTIC_FINDINGS]
        ]
        return {
            "status": "ok",
            "provider": result.provider,
            "model": result.model,
            "is_mock": result.is_mock,
            "message": None,
            "summary": (report.summary or "").strip() or None,
            "findings": findings,
        }
    return _semantic_unavailable(last_problem, code="llm_bad_output")


def _semantic_unavailable(message: str, *, code: str | None) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "provider": "agnes",
        "model": None,
        "is_mock": False,
        "message": message,
        "code": code,
        "summary": None,
        "findings": [],
    }


# ── orchestration + persistence ────────────────────────────────────────
def _overall(findings: list[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in findings}
    if "error" in severities:
        return "failed"
    if "warning" in severities:
        return "warnings"
    return "passed"


def _assemble(
    digest: str | None,
    deterministic: list[dict[str, Any]],
    semantic: dict[str, Any] | None,
) -> dict[str, Any]:
    semantic_list = list((semantic or {}).get("findings") or [])
    return {
        "status": _overall([*deterministic, *semantic_list]),
        "facts_digest": digest,
        "checked_at": utcnow().isoformat(),
        "deterministic": {
            "status": _overall(deterministic),
            "findings": deterministic,
        },
        "semantic": semantic,
    }


def _load_extraction(record: FactSheetRecord) -> dict[str, Any]:
    try:
        value = json.loads(record.extraction_json) if record.extraction_json else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _store(record: FactSheetRecord, validation: dict[str, Any]) -> None:
    extraction = _load_extraction(record)
    extraction["validation"] = validation
    record.extraction_json = json.dumps(extraction, ensure_ascii=False)


def stored_validation(record: FactSheetRecord) -> dict[str, Any] | None:
    """The persisted validation for a sheet, with a computed `stale` flag."""
    validation = _load_extraction(record).get("validation")
    if not isinstance(validation, dict):
        return None
    sheet, _ = schema_findings(_draft(record))
    current = facts_digest(sheet) if sheet is not None else None
    semantic = validation.get("semantic")
    out = dict(validation)
    out["stale"] = validation.get("facts_digest") != current
    if isinstance(semantic, dict):
        out["semantic"] = {**semantic, "stale": semantic.get("facts_digest") != current}
    return out


def _draft(record: FactSheetRecord) -> Any:
    try:
        return json.loads(record.draft_json) if record.draft_json else {}
    except ValueError:
        return None


def refresh_deterministic(record: FactSheetRecord) -> dict[str, Any]:
    """Re-run layer 1 after an edit, keeping the last semantic report.

    The semantic report keeps the digest it was computed for, so it is shown
    as stale until the owner re-runs it against the edited facts.
    """
    sheet, findings = schema_findings(_draft(record))
    digest = None
    if sheet is not None:
        findings = deterministic_findings(sheet)
        digest = facts_digest(sheet)
    previous = _load_extraction(record).get("validation")
    semantic = previous.get("semantic") if isinstance(previous, dict) else None
    current_semantic = (
        semantic
        if isinstance(semantic, dict) and semantic.get("facts_digest") == digest
        else None
    )
    validation = _assemble(digest, findings, current_semantic)
    # Keep the stale report for display; it no longer counts toward `status`.
    if current_semantic is None and isinstance(semantic, dict):
        validation["semantic"] = semantic
    _store(record, validation)
    return validation


async def validate_sheet(
    record: FactSheetRecord,
    transcript: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Run both layers and persist the result on the sheet (caller commits)."""
    settings = settings or get_settings()
    sheet, findings = schema_findings(_draft(record))
    if sheet is None:
        # Schema-invalid JSON never reaches the model.
        validation = _assemble(None, findings, None)
        _store(record, validation)
        return validation

    deterministic = deterministic_findings(sheet)
    digest = facts_digest(sheet)
    semantic = await semantic_findings(sheet, transcript, settings)
    semantic["facts_digest"] = digest
    validation = _assemble(digest, deterministic, semantic)
    _store(record, validation)
    return validation


def blocking_findings(sheet: FactSheet) -> list[dict[str, Any]]:
    """Deterministic errors that must stop a lock (beyond missing fields)."""
    return [
        item
        for item in deterministic_findings(sheet)
        if item["severity"] == "error" and item["code"] != "missing_required"
    ]


__all__ = [
    "FIELD_PATHS",
    "blocking_findings",
    "build_semantic_prompt",
    "deterministic_findings",
    "facts_digest",
    "refresh_deterministic",
    "semantic_findings",
    "stored_validation",
    "validate_sheet",
]
