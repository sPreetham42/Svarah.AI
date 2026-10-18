"""Live Agnes extraction wiring.

These tests pin the behaviour that was previously a stub: the live provider must
actually call the model, and everything it returns must pass through the
canonical normalizer. No network call is made — the LLM client is injected.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.errors import ProviderUnavailableError
from app.services.agnes import LLMResult

GOOD_PAYLOAD = {
    "business": {"name": "Sunrise Cafe", "location": None},
    "offer": {
        "product": ["cold coffee"],
        "discount_percent": 20,
        "days": ["Saturday", "Sunday"],
        "start_time": "16:00",
        "end_time": "20:00",
        # A chatty model adding an unknown key must not reach the sheet.
        "urgency": "high",
    },
    "languages": ["Hindi", "kannada", "not-a-language"],
}


class _FakeLLM:
    def __init__(self, payload, *, provider="agnes", is_mock=False):
        self.payload = payload
        self.provider = provider
        self.is_mock = is_mock
        self.calls: list[tuple[str, str]] = []

    async def complete_json(self, system, user, *, max_tokens=2000):
        self.calls.append((system, user))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return LLMResult(
            text=text, provider=self.provider, model="agnes-3.0-flash", is_mock=self.is_mock
        )


def _live_env(env_override, **extra):
    env_override(
        TITAN_MODE="live",
        TITAN_EXTRACTION_PROVIDER="agnes",
        TITAN_AGNES_API_BASE="https://agnes.invalid/v1",
        TITAN_AGNES_API_KEY="test-key-not-real",
        TITAN_AGNES_INTERFACE_VERIFIED="1",
        **extra,
    )


def test_live_extraction_calls_the_model_and_normalizes(env_override, monkeypatch):
    from app.services import extraction

    _live_env(env_override)
    fake = _FakeLLM(GOOD_PAYLOAD)
    monkeypatch.setattr(extraction, "get_llm_client", lambda settings=None: fake)

    provider = extraction.get_extraction_provider()
    assert isinstance(provider, extraction.AgnesExtractionProvider)

    result = asyncio.run(provider.extract("Sunrise Cafe: 20% off cold coffee this weekend 4-8pm"))

    assert result.provider == "agnes"
    assert result.is_mock is False
    assert result.status == "ok"

    # The model only proposes facts; the note records that they were normalized.
    facts = result.sheet
    assert facts.business.name == "Sunrise Cafe"
    assert facts.offer.product == ["cold coffee"]
    assert facts.offer.discount_percent == 20
    assert facts.offer.days == ["Saturday", "Sunday"]
    assert facts.offer.start_time == "16:00"
    assert facts.offer.end_time == "20:00"
    # Unknown keys are dropped so a chatty model cannot widen the schema.
    assert not hasattr(facts.offer, "urgency")
    # Languages are normalized ("hindi" -> "Hindi"). Unknown names are preserved
    # rather than silently dropped — that is the normalizer's documented rule.
    assert facts.languages == ["Hindi", "Kannada", "Not-a-language"]

    # The transcript is sent, and the prompt forbids inventing values.
    _, user = fake.calls[0]
    assert "cold coffee" in user


def test_live_extraction_rejects_a_non_json_completion(env_override, monkeypatch):
    from app.services import extraction

    _live_env(env_override)
    monkeypatch.setattr(
        extraction, "get_llm_client", lambda settings=None: _FakeLLM("I cannot help with that.")
    )

    provider = extraction.get_extraction_provider()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.extract("some offer"))


def test_live_extraction_rejects_facts_that_fail_validation(env_override, monkeypatch):
    from app.services import extraction

    _live_env(env_override)
    # `offer` must be an object; a scalar is a contract break, not a fact.
    monkeypatch.setattr(
        extraction, "get_llm_client", lambda settings=None: _FakeLLM({"offer": "20% off"})
    )

    provider = extraction.get_extraction_provider()
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(provider.extract("some offer"))


def test_unavailable_extraction_degrades_to_an_empty_draft(client, env_override, monkeypatch):
    """A provider outage must not break campaign creation.

    The campaign is still created (so the owner can type the facts by hand) and
    nothing is fabricated.
    """
    from app.services import extraction

    _live_env(env_override)

    def _refuse(settings=None):
        raise ProviderUnavailableError("Agnes is not configured.", code="provider_not_configured")

    monkeypatch.setattr(extraction, "get_llm_client", _refuse)

    response = client.post(
        "/api/campaigns", json={"text": "20% off cold coffee this weekend"}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["factsheet"]["status"] == "draft"
    # Every fact is empty rather than invented.
    assert body["facts"]["offer"]["discount_percent"] is None
    assert body["facts"]["offer"]["product"] == []
    assert "offer.product" in body["facts"]["missing"]
