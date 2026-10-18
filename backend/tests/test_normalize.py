"""Deterministic normalization primitive tests (Source of Truth §29)."""

from __future__ import annotations

import pytest

from app.errors import NormalizationError
from app.services.normalize import (
    coerce_number,
    normalize_date,
    normalize_day,
    normalize_days,
    normalize_language,
    normalize_time,
)


# ── numbers ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20", 20.0),
        ("20%", 20.0),
        ("20 percent", 20.0),
        ("twenty", 20.0),
        ("twenty five", 25.0),
        ("one hundred", 100.0),
        ("१०", 10.0),          # Devanagari
        ("೨೦", 20.0),          # Kannada
        ("bees", 20.0),        # Hinglish
        ("pachas", 50.0),      # Hinglish
        ("25 percent off", 25.0),
        ("₹1,999", 1999.0),
        ("rs 250", 250.0),
        (150, 150.0),
        (2.5, 2.5),
    ],
)
def test_coerce_number_supported_forms(raw, expected):
    assert coerce_number(raw) == expected


def test_coerce_number_none_and_empty():
    assert coerce_number(None) is None
    assert coerce_number("   ") is None


def test_coerce_number_rejects_unparseable():
    with pytest.raises(NormalizationError):
        coerce_number("a lot")


def test_coerce_number_rejects_boolean():
    with pytest.raises(NormalizationError):
        coerce_number(True)


# ── times ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4 PM", "16:00"),
        ("4pm", "16:00"),
        ("4:30 pm", "16:30"),
        ("16:00", "16:00"),
        ("12:00", "12:00"),    # explicit 24-hour noon, not ambiguous
        ("4:30", "04:30"),     # explicit minutes → 24-hour reading
        ("11:15 am", "11:15"),
        ("12 am", "00:00"),
        ("12 pm", "12:00"),
        ("noon", "12:00"),
        ("midnight", "00:00"),
        ("४ PM", "16:00"),     # Devanagari digits
    ],
)
def test_normalize_time_supported(raw, expected):
    assert normalize_time(raw) == expected


def test_normalize_time_ambiguous_is_rejected():
    with pytest.raises(NormalizationError):
        normalize_time("4")  # no AM/PM → ambiguous


def test_normalize_time_invalid_minutes():
    with pytest.raises(NormalizationError):
        normalize_time("4:75 pm")


# ── days ──────────────────────────────────────────────────────────────
def test_normalize_day_aliases():
    assert normalize_day("Sat") == "Saturday"
    assert normalize_day("sat.") == "Saturday"
    assert normalize_day("शनिवार") == "Saturday"   # Devanagari
    assert normalize_day("shanivar") == "Saturday"  # Romanized
    assert normalize_day("itvar") == "Sunday"


def test_normalize_days_deduplicates_and_orders():
    assert normalize_days(["Sunday", "Saturday", "sun"]) == ["Saturday", "Sunday"]
    assert normalize_days(None) == []
    assert normalize_days([]) == []


def test_normalize_day_unknown_raises():
    with pytest.raises(NormalizationError):
        normalize_day("someday")


# ── dates ─────────────────────────────────────────────────────────────
def test_normalize_date_iso():
    assert normalize_date("2026-10-10") == "2026-10-10"


def test_normalize_date_day_month():
    assert normalize_date("10 October 2026") == "2026-10-10"
    assert normalize_date("10th October") == "10 October"
    assert normalize_date("1 Nov 2026") == "2026-11-01"


def test_normalize_date_month_day():
    assert normalize_date("October 10, 2026") == "2026-10-10"


def test_normalize_date_ambiguous_rejected():
    with pytest.raises(NormalizationError):
        normalize_date("10/11/2026")


def test_normalize_date_invalid_calendar_rejected():
    with pytest.raises(NormalizationError):
        normalize_date("2026-02-30")


def test_normalize_language_aliases_and_passthrough():
    assert normalize_language("kn") == "Kannada"
    assert normalize_language("Hindi") == "Hindi"
    assert normalize_language("tulu") == "Tulu"  # unknown preserved, not dropped
    assert normalize_language(None) is None
