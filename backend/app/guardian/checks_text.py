"""Guardian text checks (Source of Truth §26–§28).

Deterministic, order-free checks against the locked token map:

- scene metadata: internal storyboard labels (``Scene 1:``, ``Shot 2 -``) are
  structural metadata, not customer-facing copy — the label token is stripped
  before analysis and only the remainder of the line is checked, so fabricated
  numbers inside a scene line still fail;
- numeric parity: every number in the copy maps to a locked token value or a
  documented safe derivation (day count);
- unresolved tokens: ``{{NAME}}`` sequences left in final copy;
- unsupported claims: fabricated scarcity/guarantees/superlatives/reviews that
  are not present as locked conditions;
- required-fact parity: a channel that renders a fact class must agree with the
  locked value (e.g. a weekday name that is not a locked day fails).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Safe derivations: stating "2 days" is fine when the locked day set has exactly
# two days. Everything else numeric must come from the token map.
_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

_CLAIM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"first\s+\d+\s+customers", "fabricated_first_n_customers"),
    (r"limited\s+stock", "fabricated_scarcity"),
    (r"while\s+stocks?\s+last", "fabricated_scarcity"),
    (r"hurry\b", "fabricated_urgency"),
    (r"today\s+only", "fabricated_urgency"),
    (r"\bguarantee(d)?\b", "fabricated_guarantee"),
    (r"\bno\.?\s*1\b", "fabricated_superlative"),
    (r"\bbest\s+in\s+town\b", "fabricated_superlative"),
    (r"award[- ]?winning", "fabricated_award"),
    (r"\bcertified\b", "fabricated_certification"),
    (r"free\s+\w+", "fabricated_freebie"),
    (r"\bcomplimentary\b", "fabricated_freebie"),
    (r"5[- ]?star\s+reviews?", "fabricated_reviews"),
    (r"thousands\s+of\s+customers", "fabricated_social_proof"),
)

_TOKEN_RESIDUE = re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}")

_NUMBER = re.compile(r"\d[\d.,]*")

#: Internal scene/storyboard labels. These are production metadata (what the
#: reel compositor/editor keys off), never a claim seen by a customer, so the
#: label prefix is removed before checks. Only the LABEL is removed — any
#: numbers in the rest of the line remain fully subject to numeric parity.
#: A label counts at the start of a line or straight after a sentence end: the
#: live model often writes every scene on one line ("\u2026 drinks. Scene 2: Cut\u2026").
_SCENE_LABEL = re.compile(
    r"(?m)(?:^[ \t]*|(?<=[.!?]\s))(?:scene|shot|frame|beat)\s+\d+\s*[:.\-\u2014]\s*",
    re.IGNORECASE,
)


def strip_scene_metadata(text: str) -> tuple[str, list[str]]:
    """Return ``(checked_text, stripped_labels)`` for a scene/storyboard script."""
    labels = [m.group(0).strip() for m in _SCENE_LABEL.finditer(text)]
    if not labels:
        return text, []
    return _SCENE_LABEL.sub("", text), labels


@dataclass
class CheckOutcome:
    check: str
    verdict: str  # PASS | FAIL | WARN | NEEDS_REVIEW
    confidence: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "details": self.details,
        }


def _token_numbers(tokens: dict[str, str]) -> set[str]:
    numbers: set[str] = set()
    for value in tokens.values():
        for match in _NUMBER.finditer(value):
            numbers.add(match.group(0).replace(",", "").strip())
    return numbers


def check_numeric_parity(text: str, tokens: dict[str, str]) -> CheckOutcome:
    """Every number must map to a locked value or safe derivation."""
    allowed = _token_numbers(tokens)
    day_count = len([d for d in tokens.get("DAYS", "").split("&") if d.strip()])
    violations: list[str] = []
    for match in _NUMBER.finditer(text):
        raw = match.group(0).replace(",", "").strip()
        if raw in allowed:
            continue
        # Safe derivation: a counted number of locked weekdays.
        if day_count and raw == str(day_count):
            continue
        violations.append(raw)
    if violations:
        return CheckOutcome(
            "numeric_parity",
            "FAIL",
            1.0,
            {"unmapped_numbers": sorted(set(violations)), "allowed": sorted(allowed)},
        )
    return CheckOutcome("numeric_parity", "PASS")


def check_unresolved_tokens(text: str, tokens: dict[str, str]) -> CheckOutcome:
    residue = _TOKEN_RESIDUE.findall(text)
    if residue:
        return CheckOutcome(
            "token_residue", "FAIL", 1.0, {"residue": residue}
        )
    return CheckOutcome("token_residue", "PASS")


def check_unsupported_claims(text: str, conditions: list[str] | None = None) -> CheckOutcome:
    """Claims must be either absent or explicitly locked as conditions."""
    conditions_blob = " ".join(conditions or []).lower()
    hits: list[dict[str, str]] = []
    for pattern, label in _CLAIM_PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            matched = match.group(0)
            if matched.lower() in conditions_blob:
                continue  # explicitly locked condition — allowed
            hits.append({"claim": label, "text": matched})
    if hits:
        return CheckOutcome(
            "unsupported_claims",
            "FAIL",
            1.0,
            {"claims": hits, "note": "Claims must exist verbatim in locked conditions to be allowed."},
        )
    return CheckOutcome("unsupported_claims", "PASS")


def check_required_facts(text: str, tokens: dict[str, str]) -> CheckOutcome:
    """If a fact class appears, its value must match the locked token.

    - weekday names present must all be locked days;
    - a percent/price-like number is covered by numeric parity.
    """
    if tokens.get("DAYS"):
        locked_days = {
            day.strip().lower()
            for day in re.split(r"&|,", tokens["DAYS"])
            if day.strip()
        }
        for name in _WEEKDAY_NAMES:
            if re.search(rf"\b{name}\b", text, re.IGNORECASE):
                if name not in locked_days:
                    return CheckOutcome(
                        "fact_parity_days",
                        "FAIL",
                        1.0,
                        {"weekday": name, "locked_days": sorted(locked_days)},
                    )
    return CheckOutcome("fact_parity_days", "PASS")


def verify_text(text: str, tokens: dict[str, str], *, conditions: list[str] | None = None) -> list[CheckOutcome]:
    """Run the deterministic checks over customer-facing copy.

    Internal scene labels are stripped first and the fact that they were
    ignored is recorded as evidence on the numeric-parity outcome.
    """
    checked_text, scene_labels = strip_scene_metadata(text)
    outcomes = [
        check_numeric_parity(checked_text, tokens),
        check_unresolved_tokens(checked_text, tokens),
        check_unsupported_claims(checked_text, conditions),
        check_required_facts(checked_text, tokens),
    ]
    if scene_labels:
        outcomes[0].details["scene_labels_ignored"] = scene_labels
    return outcomes


__all__ = [
    "CheckOutcome",
    "verify_text",
    "check_numeric_parity",
    "check_unresolved_tokens",
    "check_unsupported_claims",
    "check_required_facts",
    "strip_scene_metadata",
]
