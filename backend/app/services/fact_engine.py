"""Deterministic Fact Engine (Source of Truth §16, §17, §29).

Responsibilities:

1. normalize draft values into canonical form (never inventing anything);
2. project the *authoritative* fields into a canonical payload;
3. deterministic JSON serialization (sorted keys, stable list order);
4. SHA-256 over the canonical payload;
5. HMAC-SHA256 integrity seal with a dedicated secret;
6. deterministic Fact Token compilation and substitution.

Hash coverage — exactly these fields::

    business.name, business.location
    offer.product, offer.discount_percent, offer.discount_flat, offer.price,
    offer.quantity, offer.audience, offer.days, offer.date_start,
    offer.date_end, offer.start_time, offer.end_time, offer.conditions,
    offer.location
    languages

Mutable extraction metadata (`extraction_confidence`, `inferred`,
`ambiguities`, `missing`) is *excluded* from the canonical payload and the
hash. Integral floats are coerced to ints so `20` and `20.0` serialize
identically.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ..errors import NormalizationError, SealUnavailableError, TokenError
from .fact_schemas import Ambiguity, FactSheet
from .normalize import (
    WEEKDAY_INDEX,
    clean_text,
    coerce_number,
    normalize_date,
    normalize_days,
    normalize_language_list,
    normalize_string_list,
    normalize_time,
)

SEAL_ALGORITHM = "hmac-sha256"
HASH_ALGORITHM = "sha256"

#: Token name → source fact paths. Tokens are compiled only when their
#: supporting facts are present; absent facts never get invented display values.
TOKEN_FIELDS: dict[str, tuple[str, ...]] = {
    "PRODUCT": ("offer.product",),
    "DISCOUNT": ("offer.discount_percent", "offer.discount_flat"),
    "PRICE": ("offer.price",),
    "DAYS": ("offer.days",),
    "WINDOW": ("offer.start_time", "offer.end_time"),
    "AUDIENCE": ("offer.audience",),
    "CONDITIONS": ("offer.conditions",),
    "LOCATION": ("offer.location", "business.location"),
}

TOKEN_NAMES = frozenset(TOKEN_FIELDS)

_TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

#: Paths that block locking when missing (minimum viable, truthful offer).
HARD_REQUIRED_PATHS = frozenset(
    {"offer.product", "offer.discount_percent", "offer.discount_flat", "offer.price"}
)


# ── normalization of a full FactSheet ─────────────────────────────────
def normalize_fact_data(data: dict[str, Any] | None) -> FactSheet:
    """Normalize raw fact data and validate it against the FactSheet schema.

    Raises `NormalizationError` for anything ambiguous or unsupported rather
    than guessing.
    """
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise NormalizationError("FactSheet data must be an object.")

    data = copy.deepcopy(data)
    business = data.get("business") or {}
    if not isinstance(business, dict):
        raise NormalizationError("'business' must be an object.")
    business["name"] = clean_text(business.get("name"))
    business["location"] = clean_text(business.get("location"))

    offer = data.get("offer") or {}
    if not isinstance(offer, dict):
        raise NormalizationError("'offer' must be an object.")
    for field in ("discount_percent", "discount_flat", "price", "quantity"):
        offer[field] = coerce_number(offer.get(field))
    offer["product"] = normalize_string_list(offer.get("product"))
    offer["audience"] = normalize_string_list(offer.get("audience"))
    offer["conditions"] = normalize_string_list(offer.get("conditions"))
    offer["days"] = normalize_days(offer.get("days"))
    offer["date_start"] = normalize_date(offer.get("date_start"))
    offer["date_end"] = normalize_date(offer.get("date_end"))
    offer["start_time"] = normalize_time(offer.get("start_time"))
    offer["end_time"] = normalize_time(offer.get("end_time"))
    offer["location"] = clean_text(offer.get("location"))

    data["languages"] = normalize_language_list(data.get("languages"))

    confidence = data.get("extraction_confidence") or {}
    if not isinstance(confidence, dict):
        raise NormalizationError("'extraction_confidence' must be an object.")
    try:
        data["extraction_confidence"] = {
            str(key): float(value) for key, value in confidence.items()
        }
    except (TypeError, ValueError) as exc:
        raise NormalizationError("Confidence values must be numbers.") from exc

    data["inferred"] = [p for p in normalize_string_list(data.get("inferred"))]

    raw_ambiguities = data.get("ambiguities") or []
    if not isinstance(raw_ambiguities, list):
        raise NormalizationError("'ambiguities' must be a list.")
    ambiguities: list[dict[str, Any]] = []
    for item in raw_ambiguities:
        if isinstance(item, Ambiguity):
            ambiguities.append(item.model_dump())
        elif isinstance(item, dict):
            ambiguities.append(
                {
                    "field": str(item.get("field") or "").strip(),
                    "note": str(item.get("note") or "").strip(),
                    "candidates": [
                        c for c in normalize_string_list(item.get("candidates"))
                    ],
                }
            )
        else:
            raise NormalizationError("Each ambiguity must be an object.")
    data["ambiguities"] = ambiguities

    try:
        sheet = FactSheet.model_validate(data)
    except PydanticValidationError as exc:
        raise NormalizationError(
            "FactSheet failed validation.", details=exc.errors()
        ) from exc

    try:
        sheet = sheet.model_copy(update={"missing": recompute_missing(sheet)})
    except PydanticValidationError as exc:  # pragma: no cover - defensive
        raise NormalizationError(
            "FactSheet missing-field computation failed.", details=exc.errors()
        ) from exc
    return sheet


def recompute_missing(sheet: FactSheet) -> list[str]:
    """Deterministic list of absent information (hard blockers + soft gaps)."""
    offer = sheet.offer
    missing: list[str] = []

    if not offer.product:
        missing.append("offer.product")
    if (
        offer.discount_percent is None
        and offer.discount_flat is None
        and offer.price is None
    ):
        missing += ["offer.discount_percent", "offer.discount_flat", "offer.price"]

    if not offer.days and not (offer.date_start or offer.date_end):
        missing.append("offer.days")
    if offer.date_start and not offer.date_end:
        missing.append("offer.date_end")
    if offer.date_end and not offer.date_start:
        missing.append("offer.date_start")

    if offer.start_time and not offer.end_time:
        missing.append("offer.end_time")
    if offer.end_time and not offer.start_time:
        missing.append("offer.start_time")

    if not offer.audience:
        missing.append("offer.audience")
    if not sheet.languages:
        missing.append("languages")
    if not sheet.business.name:
        missing.append("business.name")
    if not offer.location and not sheet.business.location:
        missing.append("offer.location")
    return missing


def lock_blockers(sheet: FactSheet) -> list[str]:
    return [path for path in recompute_missing(sheet) if path in HARD_REQUIRED_PATHS]


# ── canonical payload / serialization ─────────────────────────────────
def _num(value: float | None) -> int | float | None:
    if value is None:
        return None
    if float(value).is_integer():
        return int(value)
    return float(value)


def _sorted_text(items: list[str]) -> list[str]:
    return sorted(items, key=lambda s: (s.casefold(), s))


def canonical_payload(sheet: FactSheet) -> dict[str, Any]:
    """Project the authoritative facts into a deterministic structure."""
    offer = sheet.offer
    return {
        "business": {
            "name": sheet.business.name,
            "location": sheet.business.location,
        },
        "offer": {
            "product": _sorted_text(offer.product),
            "discount_percent": _num(offer.discount_percent),
            "discount_flat": _num(offer.discount_flat),
            "price": _num(offer.price),
            "quantity": _num(offer.quantity),
            "audience": _sorted_text(offer.audience),
            "days": sorted(offer.days, key=lambda d: (WEEKDAY_INDEX.get(d, 99), d)),
            "date_start": offer.date_start,
            "date_end": offer.date_end,
            "start_time": offer.start_time,
            "end_time": offer.end_time,
            "conditions": _sorted_text(offer.conditions),
            "location": offer.location,
        },
        "languages": _sorted_text(sheet.languages),
    }


def canonical_json(payload: dict[str, Any]) -> str:
    """Byte-for-byte canonical JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_fact_hash(canonical: str) -> str:
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{HASH_ALGORITHM}:{digest}"


def compute_seal(canonical: str, secret: str | None) -> str:
    """HMAC-SHA256 seal over the canonical payload. Fails safely without a secret."""
    if not secret or not secret.strip():
        raise SealUnavailableError(
            "Fact locking is disabled: TITAN_SEAL_SECRET is not configured.",
            details={"configured": False},
        )
    digest = hmac.new(
        secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{SEAL_ALGORITHM}:{digest}"


def verify_seal(canonical: str, seal: str, secret: str | None) -> bool:
    """Constant-time seal verification."""
    expected = compute_seal(canonical, secret)
    return hmac.compare_digest(expected, seal)


def verify_fact_hash(canonical: str, fact_hash: str) -> bool:
    return hmac.compare_digest(compute_fact_hash(canonical), fact_hash)


def verify_locked_payload(
    payload: dict[str, Any], fact_hash: str | None, seal: str | None, secret: str | None
) -> bool | None:
    """Recompute hash + seal for a stored payload.

    Returns None when no secret is configured (verification impossible), so the
    API can report `seal_valid: null` instead of guessing.
    """
    if not secret or not secret.strip():
        return None
    if not fact_hash or not seal:
        return False
    canonical = canonical_json(payload)
    return verify_fact_hash(canonical, fact_hash) and verify_seal(canonical, seal, secret)


# ── Fact Tokens ───────────────────────────────────────────────────────
def fmt_number(value: float | int) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{float(value):.2f}".rstrip("0").rstrip(".")


def fmt_time(hhmm: str) -> str:
    """12-hour display for a normalized 24-hour time (e.g. `16:00` → `4 PM`)."""
    hour, _, minute = hhmm.partition(":")
    hour_i = int(hour)
    minute_i = int(minute or 0)
    meridiem = "AM" if hour_i < 12 else "PM"
    display_hour = hour_i % 12 or 12
    if minute_i:
        return f"{display_hour}:{minute_i:02d} {meridiem}"
    return f"{display_hour} {meridiem}"


def _join_list(items: list[str], *, conj: str = "&") -> str:
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} {conj} {items[-1]}"


def compile_tokens(payload: dict[str, Any]) -> dict[str, str]:
    """Compile the deterministic Fact Token map from authoritative facts.

    Only tokens whose facts exist are compiled. No display value is invented
    for absent facts.
    """
    offer = payload.get("offer", {})
    business = payload.get("business", {})
    tokens: dict[str, str] = {}

    if offer.get("product"):
        tokens["PRODUCT"] = ", ".join(offer["product"])
    if offer.get("discount_percent") is not None:
        tokens["DISCOUNT"] = f"{fmt_number(offer['discount_percent'])}%"
    elif offer.get("discount_flat") is not None:
        tokens["DISCOUNT"] = f"{fmt_number(offer['discount_flat'])} off"
    if offer.get("price") is not None:
        tokens["PRICE"] = fmt_number(offer["price"])
    if offer.get("days"):
        tokens["DAYS"] = _join_list(offer["days"])
    if offer.get("start_time") and offer.get("end_time"):
        tokens["WINDOW"] = f"{fmt_time(offer['start_time'])}–{fmt_time(offer['end_time'])}"
    if offer.get("audience"):
        tokens["AUDIENCE"] = ", ".join(offer["audience"])
    if offer.get("conditions"):
        tokens["CONDITIONS"] = ", ".join(offer["conditions"])
    location = offer.get("location") or business.get("location")
    if location:
        tokens["LOCATION"] = location
    return tokens


def validate_template(template: str, tokens: dict[str, str]) -> list[str]:
    """Return human-readable issues for unknown or unresolved tokens."""
    issues: list[str] = []
    for match in _TOKEN_PATTERN.finditer(template):
        name = match.group(1).upper()
        if name in tokens:
            continue
        if name in TOKEN_NAMES:
            issues.append(f"Token {{{{{name}}}}} is known but unresolved by the facts.")
        else:
            issues.append(f"Unknown token {{{{{name}}}}}.")
    return issues


def substitute_tokens(template: str, tokens: dict[str, str]) -> str:
    """Deterministically replace tokens. Unknown/unresolved tokens raise."""
    issues = validate_template(template, tokens)
    if issues:
        raise TokenError("Template contains invalid tokens.", details={"issues": issues})

    def _replace(match: re.Match[str]) -> str:
        return tokens[match.group(1).upper()]

    return _TOKEN_PATTERN.sub(_replace, template)


def canonical_json_from_stored(facts_json: str) -> str:
    """Re-serialize a stored payload into canonical bytes deterministically."""
    return canonical_json(json.loads(facts_json))
