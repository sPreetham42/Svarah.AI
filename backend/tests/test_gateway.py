"""Provider gateway tests: breaker, retry/backoff, status reporting."""

from __future__ import annotations

import asyncio

import pytest

from app.errors import ProviderUnavailableError
from app.services.gateway import (
    CircuitBreaker,
    ProviderError,
    build_gateway,
    with_retry,
)


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, recovery_time=0.01)
    assert breaker.state == "closed"
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow_request() is False

    import time

    time.sleep(0.02)
    assert breaker.state == "half-open"
    assert breaker.allow_request() is True
    breaker.record_success()
    assert breaker.state == "closed"


def test_with_retry_retries_retryable_errors():
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ProviderError("temporary", provider="p", retryable=True)
        return "ok"

    result = asyncio.run(
        with_retry(flaky, provider="p", max_retries=3, backoff_base=0.0, timeout=5)
    )
    assert result == "ok"
    assert attempts["n"] == 3


def test_with_retry_does_not_retry_permanent_errors():
    attempts = {"n": 0}

    async def broken():
        attempts["n"] += 1
        raise ProviderError("permanent", provider="p", retryable=False)

    with pytest.raises(ProviderError):
        asyncio.run(
            with_retry(broken, provider="p", max_retries=3, backoff_base=0.0, timeout=5)
        )
    assert attempts["n"] == 1


def test_with_retry_gives_up_with_normalized_error():
    async def always_timeout():
        await asyncio.sleep(0.05)

    with pytest.raises(ProviderUnavailableError):
        asyncio.run(
            with_retry(
                always_timeout,
                provider="slow",
                max_retries=1,
                backoff_base=0.0,
                timeout=0.01,
            )
        )


def test_gateway_reports_unverified_providers_honestly(client):
    from app.config import get_settings

    gateway = build_gateway(get_settings())
    report = {p.name: p for p in gateway.status_report()}
    assert {"agnes_llm", "agnes_image", "agnes_video", "voice", "publisher"} <= set(report)
    for status in report.values():
        assert status.verified is False
        assert "unverified" in status.detail.lower() or not status.configured


def test_gateway_refuses_unverified_calls():
    from app.config import get_settings

    gateway = build_gateway(get_settings())
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(gateway.call_unverified("agnes_llm"))


def test_gateway_marks_agnes_configured_when_env_present(client, env_override):
    env_override(TITAN_AGNES_API_BASE="https://example.invalid", TITAN_AGNES_API_KEY="k")
    from app.config import get_settings

    gateway = build_gateway(get_settings())
    agnes = next(p for p in gateway.status_report() if p.name == "agnes_llm")
    assert agnes.configured is True
    # Still not verified/available until the real interface is confirmed.
    assert agnes.verified is False
    assert agnes.available is False
