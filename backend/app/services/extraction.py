"""Fact extraction providers (Source of Truth §13, §14).

Extraction *proposes*; it never locks. The provider hierarchy is:

    Agnes 3.0 Flash (via the provider gateway)
        -> (when unavailable / not configured) manual FactSheet entry,
           surfaced to the client as an in-band "unavailable" status.

A live Agnes call needs `TITAN_MODE=live` and a configured key; when either is
missing the call raises `ProviderUnavailableError` and the API degrades to
manual entry rather than fabricating facts. Whatever the model returns is
passed through `normalize_fact_data` (the deterministic schema), so a chatty or
malformed response can never become a locked fact. Extraction is followed by
`fact_validation` (deterministic rules + Agnes semantic checks). Mock
extraction is deterministic, rule-based and explicitly labelled
(`is_mock=True`).
"""

from __future__ import annotations

import abc
import json
import re
from dataclasses import dataclass
from typing import Any

from ..config import Settings, get_settings
from ..errors import (
    ExtractionUnavailableError,
    NormalizationError,
    ProviderUnavailableError,
)
from .agnes import LLMResult, get_llm_client, parse_llm_json
from .fact_engine import normalize_fact_data
from .fact_schemas import FactSheet
from .gateway import ProviderGateway, ProviderStatus, build_gateway
from .normalize import clean_text, coerce_number, normalize_days, normalize_language

# ── vocabularies for the deterministic mock extractor ─────────────────
_PRODUCT_VOCABULARY = (
    "cold coffee", "coffee", "iced tea", "tea", "pizza", "burger", "sandwich",
    "pasta", "noodles", "biryani", "dosa", "idli", "thali", "cake", "pastry",
    "ice cream", "milkshake", "smoothie", "haircut", "hair cut", "beard trim",
    "massage", "facial", "manicure", "pedicure", "saree", "kurta", "shirt",
    "jeans", "shoes", "groceries", "vegetables", "fruits", "books",
    "stationery", "phone repair", "laptop repair", "gym membership",
    "yoga class", "dance class",
)
_AUDIENCE_VOCABULARY = (
    "college students", "school students", "students", "office workers",
    "families", "kids", "children", "seniors", "teenagers", "women", "men",
    "couples", "locals", "neighbours", "neighbors", "regulars", "professionals",
    "foodies",
)
_LANGUAGE_NAMES = (
    "english", "hindi", "kannada", "tamil", "telugu", "marathi", "bengali",
    "malayalam", "gujarati", "punjabi", "urdu",
)

_PERCENT_DIGIT_RE = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")
_PERCENT_WORD_RE = re.compile(
    r"\b([a-z]+(?:\s+[a-z]+)?)\s+(?:percent|per\s+cent)\b", re.IGNORECASE
)
_CURRENCY_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", re.IGNORECASE)
_RUPEES_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+rupees?\b", re.IGNORECASE)
_FLAT_OFF_RE = re.compile(r"(?:flat\s+)?([\d,]+(?:\.\d+)?)\s+off\b", re.IGNORECASE)
_QUANTITY_RE = re.compile(
    r"(\d{1,4})\s*(?:pieces?|packets?|pcs|kg|grams?|litres?|bottles?|plates?|scoops?|cups?)",
    re.IGNORECASE,
)
_TIME_MERIDIEM_RE = re.compile(r"(\d{1,2}(?::\d{2})?)\s*(am|pm)\b", re.IGNORECASE)
_TIME24_RE = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
_RANGE_SHARED_RE = re.compile(
    r"(\d{1,2}(?::\d{2})?)\s*(?:to|till|until|-|–)\s*(\d{1,2}(?::\d{2})?)\s*(am|pm)\b",
    re.IGNORECASE,
)
_WEEKEND_RE = re.compile(r"\bweekends?\b", re.IGNORECASE)
_CONDITION_RE = re.compile(r"\bfor\s+([a-z][a-z ]{1,40}?)\s+only\b", re.IGNORECASE)
_LOCATION_RE = re.compile(
    r"\bat\s+(?:our\s+|the\s+)?([a-z][a-z\- ]{1,30}?)"
    r"(?=\s*(?:[,.]|$|\s+(?:this|on|for|from|in|and)\b))",
    re.IGNORECASE,
)


@dataclass
class ExtractionResult:
    sheet: FactSheet
    provider: str
    is_mock: bool
    status: str = "ok"
    message: str | None = None
    fallback: str | None = None

    def as_status(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "provider": self.provider,
            "is_mock": self.is_mock,
            "message": self.message,
            "fallback": self.fallback,
        }


class ExtractionProvider(abc.ABC):
    name: str = "extraction"

    @abc.abstractmethod
    async def extract(
        self,
        transcript: str,
        *,
        business_name: str | None = None,
        locale: str | None = None,
    ) -> ExtractionResult:
        ...


# ── deterministic mock extractor ──────────────────────────────────────
def _find_vocabulary(text: str, vocabulary: tuple[str, ...]) -> list[str]:
    """Case-insensitive vocabulary scan that drops sub-span duplicates.

    E.g. ``cold coffee`` also contains ``coffee``; only the longest phrase at
    that position is kept.
    """
    lowered = text.lower()
    spans: list[tuple[int, int]] = []
    for term in vocabulary:
        # Allow a simple plural suffix on the final word (pizza → pizzas).
        for match in re.finditer(rf"\b{re.escape(term)}(?:s|es)?\b", lowered):
            spans.append((match.start(), match.end()))
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))

    kept: list[tuple[int, int]] = []
    for start, end in spans:
        if any(start >= ks and end <= ke for ks, ke in kept):
            continue
        kept.append((start, end))
    kept.sort()
    return [text[start:end] for start, end in kept]


def _extract_conflicting(
    values: list[float],
) -> tuple[float | None, list[str]]:
    """Return a single value or (None, candidates) when values conflict."""
    distinct = sorted({round(v, 4) for v in values})
    if len(distinct) == 1:
        return distinct[0], []
    if len(distinct) > 1:
        return None, [_pretty_number(v) for v in distinct]
    return None, []


def _pretty_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _extract_percent(text: str) -> list[float]:
    values: list[float] = []
    for match in _PERCENT_DIGIT_RE.finditer(text):
        values.append(float(match.group(1).replace(",", "")))
    for match in _PERCENT_WORD_RE.finditer(text):
        try:
            number = coerce_number(match.group(1))
        except Exception:  # noqa: BLE001 - not a number phrase
            continue
        if number is not None:
            values.append(number)
    return values


def _extract_flat_discount(text: str) -> list[float]:
    return [float(m.group(1).replace(",", "")) for m in _FLAT_OFF_RE.finditer(text)]


def _extract_price(text: str) -> list[float]:
    values = [float(m.group(1).replace(",", "")) for m in _CURRENCY_RE.finditer(text)]
    values += [float(m.group(1).replace(",", "")) for m in _RUPEES_RE.finditer(text)]
    return values


def _extract_times(text: str) -> tuple[str | None, str | None, list[str]]:
    """Return (start, end, ambiguity_notes)."""
    from .normalize import normalize_time  # local import avoids cycle concerns

    ambiguities: list[str] = []
    shared = _RANGE_SHARED_RE.search(text)
    if shared:
        meridiem = shared.group(3).lower()
        try:
            start = normalize_time(f"{shared.group(1)} {meridiem}")
            end = normalize_time(f"{shared.group(2)} {meridiem}")
            return start, end, ambiguities
        except Exception:  # noqa: BLE001
            ambiguities.append("Time range could not be normalized.")

    positions: list[tuple[int, str]] = []
    for match in _TIME_MERIDIEM_RE.finditer(text):
        try:
            value = normalize_time(match.group(0))
        except Exception:  # noqa: BLE001
            continue
        if value:
            positions.append((match.start(), value))
    for match in _TIME24_RE.finditer(text):
        try:
            value = normalize_time(match.group(0))
        except Exception:  # noqa: BLE001
            continue
        if value:
            positions.append((match.start(), value))

    positions.sort(key=lambda item: item[0])
    ordered: list[str] = []
    for _, value in positions:
        if value not in ordered:
            ordered.append(value)

    if len(ordered) >= 2:
        return ordered[0], ordered[1], ambiguities
    if len(ordered) == 1:
        index = positions[0][0]
        prefix = text[max(0, index - 16):index].lower()
        if any(word in prefix for word in ("until", "till", "upto", "up to")):
            return None, ordered[0], ambiguities
        return ordered[0], None, ambiguities
    return None, None, ambiguities


def _extract_days(text: str) -> list[str]:
    from .normalize import _DAY_ALIASES  # shared alias table

    lowered = text.lower()
    aliases = sorted(_DAY_ALIASES, key=len, reverse=True)
    matches: list[str] = []
    for alias in aliases:
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered):
            matches.append(alias)
    return normalize_days(matches) if matches else []


def _rules_extract(
    transcript: str, *, business_name: str | None
) -> FactSheet:
    text = transcript
    confidence: dict[str, float] = {}
    inferred: list[str] = []
    ambiguities: list[dict[str, Any]] = []

    offer: dict[str, Any] = {}

    percent, percent_conflict = _extract_conflicting(_extract_percent(text))
    if percent_conflict:
        ambiguities.append(
            {
                "field": "offer.discount_percent",
                "note": "Multiple discount percentages were mentioned.",
                "candidates": [f"{c}%" for c in percent_conflict],
            }
        )
    elif percent is not None:
        offer["discount_percent"] = percent
        confidence["offer.discount_percent"] = 0.95

    flat, flat_conflict = _extract_conflicting(_extract_flat_discount(text))
    if flat_conflict:
        ambiguities.append(
            {
                "field": "offer.discount_flat",
                "note": "Multiple flat discount amounts were mentioned.",
                "candidates": flat_conflict,
            }
        )
    elif flat is not None:
        offer["discount_flat"] = flat
        confidence["offer.discount_flat"] = 0.9

    price, price_conflict = _extract_conflicting(_extract_price(text))
    if price_conflict:
        ambiguities.append(
            {
                "field": "offer.price",
                "note": "Multiple prices were mentioned.",
                "candidates": price_conflict,
            }
        )
    elif price is not None:
        offer["price"] = price
        confidence["offer.price"] = 0.9

    quantity_match = _QUANTITY_RE.search(text)
    if quantity_match:
        offer["quantity"] = float(quantity_match.group(1))
        confidence["offer.quantity"] = 0.8

    products = _find_vocabulary(text, _PRODUCT_VOCABULARY)
    if products:
        offer["product"] = products
        confidence["offer.product"] = 0.7

    audience = _find_vocabulary(text, _AUDIENCE_VOCABULARY)
    if audience:
        offer["audience"] = audience
        confidence["offer.audience"] = 0.7

    languages = _find_vocabulary(text, _LANGUAGE_NAMES)
    normalized_languages = [
        lang for lang in (normalize_language(item) for item in languages) if lang
    ]

    days = _extract_days(text)
    if _WEEKEND_RE.search(text):
        weekend_days = normalize_days(["Saturday", "Sunday"])
        contributed = [day for day in weekend_days if day not in days]
        if contributed:
            explicit_days = bool(days)
            days = normalize_days([*days, *contributed])
            inferred.append("offer.days")
            confidence["offer.days"] = 0.7 if explicit_days else 0.6
    if days:
        offer["days"] = days
        confidence.setdefault("offer.days", 0.9)

    start_time, end_time, time_notes = _extract_times(text)
    if start_time:
        offer["start_time"] = start_time
        confidence["offer.start_time"] = 0.9
    if end_time:
        offer["end_time"] = end_time
        confidence["offer.end_time"] = 0.9
    for note in time_notes:
        ambiguities.append({"field": "offer.start_time", "note": note, "candidates": []})

    condition_match = _CONDITION_RE.search(text)
    if condition_match:
        offer["conditions"] = [f"for {condition_match.group(1).strip()} only"]
        confidence["offer.conditions"] = 0.7

    location_match = _LOCATION_RE.search(text)
    if location_match:
        offer["location"] = clean_text(location_match.group(1))
        if offer["location"]:
            confidence["offer.location"] = 0.6

    raw: dict[str, Any] = {
        "business": {"name": business_name, "location": None},
        "offer": offer,
        "languages": normalized_languages,
        "extraction_confidence": confidence,
        "inferred": inferred,
        "ambiguities": ambiguities,
    }
    return normalize_fact_data(raw)


class MockExtractionProvider(ExtractionProvider):
    """Deterministic, rule-based extraction — explicitly labelled as mock."""

    name = "mock"

    async def extract(
        self,
        transcript: str,
        *,
        business_name: str | None = None,
        locale: str | None = None,
    ) -> ExtractionResult:
        sheet = _rules_extract(transcript, business_name=business_name)
        return ExtractionResult(
            sheet=sheet,
            provider=self.name,
            is_mock=True,
            status="ok",
            message=(
                "Extracted by the deterministic mock provider. "
                "Review and correct every field before locking."
            ),
        )


# ── live extraction prompt ─────────────────────────────────────────────
_EXTRACTION_SYSTEM = """You extract structured facts from a small-business promotion.

Return ONLY a JSON object. Extract a value only when the speaker actually stated
it: never invent, guess, round, or embellish. Use null for unknown scalars and
[] for unknown lists, and do not add fields that were not stated.

{
  "business": {"name": string|null, "location": string|null},
  "offer": {
    "product": string[],
    "discount_percent": number|null,
    "discount_flat": number|null,
    "price": number|null,
    "quantity": number|null,
    "audience": string[],
    "days": string[],
    "date_start": string|null,
    "date_end": string|null,
    "start_time": string|null,
    "end_time": string|null,
    "conditions": string[],
    "location": string|null
  },
  "languages": string[]
}

Rules:
- product: the exact words used for what is on offer, e.g. ["cold coffee"].
- discount_percent: the number only, e.g. "20% off" -> 20.
- discount_flat: a flat amount, e.g. "50 off" -> 50.
- price: a stated price as a number, with no currency symbol.
- days: full English weekday names, e.g. ["Saturday","Sunday"]. "this weekend"
  means ["Saturday","Sunday"].
- date_start / date_end: YYYY-MM-DD, only when an explicit date is stated.
- start_time / end_time: 24-hour HH:MM, e.g. "4 to 8 PM" -> "16:00" and "20:00".
- conditions: stated conditions in the speaker's own words.
- languages: only languages explicitly asked for, e.g. ["Hindi"].
"""

#: Offer keys the canonical schema understands; anything else is dropped.
_OFFER_FIELDS = (
    "product", "discount_percent", "discount_flat", "price", "quantity",
    "audience", "days", "date_start", "date_end", "start_time", "end_time",
    "conditions", "location",
)


def build_extraction_prompt(
    transcript: str, *, business_name: str | None = None
) -> tuple[str, str]:
    envelope: dict[str, Any] = {"transcript": transcript}
    if business_name:
        envelope["known_business_name"] = business_name
    return _EXTRACTION_SYSTEM, json.dumps(envelope, ensure_ascii=False)


def _coerce_extraction_payload(
    parsed: dict[str, Any], *, business_name: str | None
) -> dict[str, Any]:
    """Trim a model response to the fact schema before normalization.

    Unknown keys are dropped so a chatty model cannot smuggle extra fields into
    the canonical sheet; everything left still has to survive
    `normalize_fact_data`.
    """
    if not isinstance(parsed, dict):
        raise ProviderUnavailableError(
            "Agnes extraction did not return a JSON object.", code="llm_bad_output"
        )

    def _section(key: str) -> dict[str, Any]:
        """A missing/null section is fine; a wrong type is a contract break."""
        value = parsed.get(key)
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        raise ProviderUnavailableError(
            f"Agnes extraction returned '{key}' as {type(value).__name__}, not an object.",
            code="llm_bad_output",
        )

    business = _section("business")
    # The caller may already know the business name; the model must not invent one.
    if business_name and not clean_text(business.get("name")):
        business["name"] = business_name
    offer = _section("offer")
    languages = parsed.get("languages")
    return {
        "business": business,
        "offer": {k: v for k, v in offer.items() if k in _OFFER_FIELDS},
        "languages": languages if isinstance(languages, list) else [],
    }


class AgnesExtractionProvider(ExtractionProvider):
    """Agnes 3.0 Flash extraction (real model call, gateway-gated).

    The model only *proposes* facts. Its JSON is coerced to the schema, then run
    through `normalize_fact_data`, which rejects anything malformed — so a bad
    completion degrades to manual entry instead of becoming a locked fact.
    """

    name = "agnes"

    def __init__(
        self, settings: Settings, gateway: ProviderGateway | None = None
    ) -> None:
        self._settings = settings
        self._gateway = gateway or build_gateway(settings)

    async def extract(
        self,
        transcript: str,
        *,
        business_name: str | None = None,
        locale: str | None = None,
    ) -> ExtractionResult:
        settings = self._settings

        if settings.is_mock:
            # `TITAN_EXTRACTION_PROVIDER=agnes` while offline stays deterministic
            # and labelled, rather than echoing the mock LLM's own envelope.
            sheet = _rules_extract(transcript, business_name=business_name)
            return ExtractionResult(
                sheet=sheet,
                provider="mock",
                is_mock=True,
                status="ok",
                message=(
                    "Extracted by the deterministic mock provider. "
                    "Review and correct every field before locking."
                ),
            )

        # Raises ProviderUnavailableError when unconfigured; the caller then
        # offers manual entry.
        client = get_llm_client(settings)
        system, user = build_extraction_prompt(transcript, business_name=business_name)
        result: LLMResult = await client.complete_json(system, user, max_tokens=1200)
        parsed = parse_llm_json(result)
        payload = _coerce_extraction_payload(parsed, business_name=business_name)
        try:
            sheet = normalize_fact_data(payload)
        except NormalizationError as exc:
            raise ProviderUnavailableError(
                "Agnes returned facts that failed validation; enter them manually.",
                code="llm_bad_facts",
                details={"error": str(exc)},
            ) from exc

        return ExtractionResult(
            sheet=sheet,
            provider=result.provider,
            is_mock=result.is_mock,
            status="ok",
            message=(
                f"Extracted by Agnes ({result.model}). Review and correct every "
                "field before locking."
            ),
        )


def get_extraction_provider(settings: Settings | None = None) -> ExtractionProvider:
    settings = settings or get_settings()
    choice = settings.extraction_provider

    if choice == "none":
        raise ExtractionUnavailableError(
            "Fact extraction is disabled (TITAN_EXTRACTION_PROVIDER=none). "
            "Enter the facts manually.",
            details={"reason": "disabled", "fallback": "manual_entry"},
        )
    if choice == "mock":
        return MockExtractionProvider()
    if choice == "agnes":
        return AgnesExtractionProvider(settings)
    # auto
    if settings.is_mock:
        return MockExtractionProvider()
    return AgnesExtractionProvider(settings)


def extraction_status(settings: Settings | None = None) -> ProviderStatus:
    settings = settings or get_settings()
    choice = settings.extraction_provider
    mode = settings.titan_mode

    if choice == "none":
        return ProviderStatus(
            name="extraction",
            kind="extraction",
            mode=mode,
            configured=False,
            available=False,
            verified=True,
            detail="Disabled by configuration; manual FactSheet entry is the fallback.",
            capabilities=["manual_entry"],
        )
    if choice == "mock" or settings.is_mock:
        return ProviderStatus(
            name="extraction",
            kind="extraction",
            mode=mode,
            configured=True,
            available=True,
            verified=True,
            detail="Mock extraction active (deterministic, rule-based, labelled is_mock).",
            capabilities=["mock_extraction"],
        )

    configured = settings.agnes_configured
    return ProviderStatus(
        name="extraction",
        kind="extraction",
        mode=mode,
        configured=configured,
        available=configured,
        verified=configured,
        detail=(
            f"Agnes {settings.agnes_text_model} extraction + semantic validation active."
            if configured
            else "Agnes is not configured: set TITAN_AGNES_API_BASE and TITAN_AGNES_API_KEY. "
            "Facts must be entered manually until then."
        ),
        capabilities=["agnes_extraction", "manual_entry"],
    )
