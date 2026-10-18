"""Fact extraction provider tests: mock labeling, conflicts, inference, availability."""

from __future__ import annotations

import asyncio

import pytest

from app.config import get_settings
from app.errors import ExtractionUnavailableError, ProviderUnavailableError
from app.services.extraction import (
    AgnesExtractionProvider,
    MockExtractionProvider,
    extraction_status,
    get_extraction_provider,
)
from app.services.fact_schemas import FactSheet

DEMO_TRANSCRIPT = (
    "Mock transcript: 20% off cold coffee this Saturday and Sunday, "
    "4 PM to 8 PM at our cafe for college students."
)


def extract(transcript: str, **kwargs):
    provider = MockExtractionProvider()
    return asyncio.run(provider.extract(transcript, **kwargs))


def test_mock_extraction_is_labelled_and_schema_valid():
    result = extract(DEMO_TRANSCRIPT)
    assert result.is_mock is True
    assert result.provider == "mock"
    assert result.status == "ok"
    assert isinstance(result.sheet, FactSheet)


def test_mock_extraction_core_facts():
    sheet = extract(DEMO_TRANSCRIPT).sheet
    assert sheet.offer.discount_percent == 20
    assert sheet.offer.discount_flat is None  # "20% off" must not become a flat discount
    assert sheet.offer.product == ["cold coffee"]
    assert sheet.offer.days == ["Saturday", "Sunday"]
    assert sheet.offer.start_time == "16:00"
    assert sheet.offer.end_time == "20:00"
    assert sheet.offer.audience == ["college students"]
    assert sheet.offer.location == "cafe"
    assert "languages" in sheet.missing  # never mentioned → reported, not invented
    assert all(0.0 <= score <= 1.0 for score in sheet.extraction_confidence.values())


def test_mock_extraction_sets_business_name_from_context():
    sheet = extract(DEMO_TRANSCRIPT, business_name="Demo Cafe").sheet
    assert sheet.business.name == "Demo Cafe"


def test_conflicting_percentages_are_not_silently_resolved():
    sheet = extract("Get 20% off on Saturday and also 25% off for students.").sheet
    assert sheet.offer.discount_percent is None
    ambiguity = next(a for a in sheet.ambiguities if a.field == "offer.discount_percent")
    assert "20%" in ambiguity.candidates and "25%" in ambiguity.candidates
    # The conflict leaves the field missing rather than guessed.
    assert "offer.discount_percent" in sheet.missing


def test_no_facts_transcript_does_not_fabricate():
    sheet = extract("We are open as usual.").sheet
    assert sheet.offer.discount_percent is None
    assert sheet.offer.price is None
    assert sheet.offer.product == []
    assert sheet.offer.days == []
    assert sheet.offer.start_time is None
    assert "offer.product" in sheet.missing


def test_weekend_is_marked_inferred():
    sheet = extract("Big discount this weekend on our special thali.").sheet
    assert sheet.offer.days == ["Saturday", "Sunday"]
    assert "offer.days" in sheet.inferred
    assert sheet.extraction_confidence["offer.days"] <= 0.7


def test_explicit_days_are_not_marked_inferred():
    sheet = extract("Offer valid only on Saturday and Sunday.").sheet
    assert sheet.offer.days == ["Saturday", "Sunday"]
    assert "offer.days" not in sheet.inferred


def test_weekend_plus_explicit_day_is_union_with_inference():
    sheet = extract("Extended this weekend, also on Friday.").sheet
    assert sheet.offer.days == ["Friday", "Saturday", "Sunday"]
    assert "offer.days" in sheet.inferred


def test_quantity_and_price_and_flat_discount():
    sheet = extract("₹499 for 2 scoops of ice cream, flat 50 off.").sheet
    assert sheet.offer.price == 499
    assert sheet.offer.quantity == 2
    assert sheet.offer.product == ["ice cream"]
    assert sheet.offer.discount_flat == 50


def test_shared_meridiem_range_normalizes_both_ends():
    sheet = extract("Sale 12 to 9 PM at our pizzeria.").sheet
    assert sheet.offer.start_time == "12:00"
    assert sheet.offer.end_time == "21:00"


def test_plural_products_are_matched():
    sheet = extract("15% off all pizzas this weekend at our pizzeria for families.").sheet
    assert sheet.offer.product == ["pizzas"]
    assert sheet.offer.discount_percent == 15
    assert sheet.offer.days == ["Saturday", "Sunday"]
    assert "offer.product" not in sheet.missing


def test_until_only_sets_end_time():
    sheet = extract("Open until 11 PM tonight.").sheet
    assert sheet.offer.end_time == "23:00"
    assert sheet.offer.start_time is None
    assert "offer.start_time" in sheet.missing


def test_extraction_disabled_raises(env_override):
    env_override(TITAN_EXTRACTION_PROVIDER="none")
    with pytest.raises(ExtractionUnavailableError):
        get_extraction_provider(get_settings())


def test_agnes_provider_live_unconfigured_raises(client, env_override):
    """A live Agnes slot without a key must refuse, not call out."""
    env_override(TITAN_MODE="live", TITAN_EXTRACTION_PROVIDER="agnes")
    provider = AgnesExtractionProvider(get_settings())
    with pytest.raises(ProviderUnavailableError) as excinfo:
        asyncio.run(provider.extract(DEMO_TRANSCRIPT))
    assert excinfo.value.code == "provider_not_configured"


def test_live_mode_with_a_key_reaches_the_real_agnes_client(client, env_override):
    """Explicit live mode + a configured key is the whole gate (no extra toggle)."""
    from app.services.agnes import AgnesLLMClient, MockLLMClient, get_llm_client

    env_override(
        TITAN_MODE="live",
        TITAN_AGNES_API_BASE="https://example.invalid",
        TITAN_AGNES_API_KEY="k",
    )
    assert isinstance(get_llm_client(get_settings()), AgnesLLMClient)

    env_override(TITAN_MODE="mock")
    assert isinstance(get_llm_client(get_settings()), MockLLMClient)


def test_agnes_provider_in_mock_mode_degrades_to_labelled_mock(client, env_override):
    """Offline, an Agnes-selected slot still produces a usable, labelled sheet.

    The mock is rule-based and carries `is_mock=True`, so the offline demo keeps
    working without ever pretending a live model answered.
    """
    env_override(TITAN_MODE="mock", TITAN_EXTRACTION_PROVIDER="agnes")
    provider = AgnesExtractionProvider(get_settings())
    result = asyncio.run(provider.extract(DEMO_TRANSCRIPT))
    assert result.is_mock is True
    assert result.provider == "mock"
    assert result.sheet.offer.discount_percent == 20


def test_provider_selection_follows_mode(client, env_override):
    env_override(TITAN_MODE="mock", TITAN_EXTRACTION_PROVIDER="auto")
    assert isinstance(get_extraction_provider(get_settings()), MockExtractionProvider)

    env_override(TITAN_MODE="live", TITAN_EXTRACTION_PROVIDER="auto")
    assert isinstance(get_extraction_provider(get_settings()), AgnesExtractionProvider)


def test_extraction_status_views(client, env_override):
    env_override(TITAN_MODE="mock", TITAN_EXTRACTION_PROVIDER="auto")
    assert extraction_status(get_settings()).available is True

    env_override(TITAN_MODE="live")
    live = extraction_status(get_settings())
    assert live.verified is False
    assert live.available is False
