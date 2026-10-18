"""Provider Gateway.

All external AI providers are reached through a single abstraction that owns:

- configuration awareness (never guesses provider APIs);
- timeouts;
- exponential backoff retries for *safe* (idempotent) operations;
- a per-provider circuit breaker;
- normalized errors;
- provider-status reporting;
- `TITAN_MODE=live|mock`.

Phase 1 intentionally registers provider *slots* rather than inventing
undocumented APIs. A slot reports whether it is configured and explicitly
marks itself `verified=False` until the real interface and credentials are
confirmed. Calling an unverified provider raises a normalized
`ProviderUnavailableError` instead of faking a result.
"""

from __future__ import annotations

import abc
import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError

T = TypeVar("T")


class ProviderError(Exception):
    """Normalized provider failure."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_error",
        provider: str | None = None,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "retryable": self.retryable,
        }


@dataclass
class ProviderStatus:
    name: str
    kind: str
    mode: str
    configured: bool
    available: bool
    verified: bool
    detail: str
    capabilities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "mode": self.mode,
            "configured": self.configured,
            "available": self.available,
            "verified": self.verified,
            "detail": self.detail,
            "capabilities": list(self.capabilities),
        }


class CircuitBreaker:
    """Minimal closed/open/half-open breaker driven by consecutive failures."""

    def __init__(self, failure_threshold: int = 5, recovery_time: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_time = recovery_time
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if time.monotonic() - self._opened_at >= self.recovery_time:
            return "half-open"
        return "open"

    def allow_request(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {"state": self.state, "failures": self._failures}


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    provider: str,
    max_retries: int = 2,
    backoff_base: float = 0.5,
    timeout: float | None = 30.0,
    breaker: CircuitBreaker | None = None,
    retry_on: tuple[type[BaseException], ...] = (ProviderError, asyncio.TimeoutError),
) -> T:
    """Run an idempotent provider operation with backoff + optional breaker.

    Only retries errors that opt in via `ProviderError.retryable` or the
    `retry_on` types. Non-retryable normalized errors surface immediately.
    """
    if breaker is not None and not breaker.allow_request():
        raise ProviderUnavailableError(
            f"Provider '{provider}' is temporarily unavailable (circuit open).",
            details={"provider": provider, "reason": "circuit_open"},
        )

    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            if timeout is not None:
                result = await asyncio.wait_for(operation(), timeout=timeout)
            else:
                result = await operation()
        except asyncio.TimeoutError as exc:
            last_exc = exc
        except ProviderError as exc:
            if not exc.retryable:
                raise
            last_exc = exc
        except Exception as exc:  # noqa: BLE001 - normalized at the boundary
            last_exc = exc
        else:
            if breaker is not None:
                breaker.record_success()
            return result

        if breaker is not None:
            breaker.record_failure()

        if attempt < max_retries:
            delay = backoff_base * (2**attempt) + random.uniform(0, backoff_base / 2)
            await asyncio.sleep(delay)

    if isinstance(last_exc, (ProviderError, ProviderUnavailableError)):
        raise last_exc
    raise ProviderUnavailableError(
        f"Provider '{provider}' failed after {max_retries + 1} attempt(s).",
        details={"provider": provider, "error": str(last_exc)},
    )


class BaseProvider(abc.ABC):
    """A named external-service slot."""

    name: str = "base"
    kind: str = "generic"
    capabilities: tuple[str, ...] = ()
    #: Mock-mode description of what stands in for the live provider.
    mock_detail: str = "Mock mode: a labelled offline provider is used."
    missing_detail: str = "Not configured."
    #: False for slots that have no live integration in this build.
    has_live_integration: bool = True

    def is_configured(self, settings: Settings) -> bool:
        return False

    def status(self, settings: Settings) -> ProviderStatus:
        configured = self.is_configured(settings)
        # A slot describes the *live* provider: in mock mode it is never
        # reported as available, even with keys present (a labelled mock
        # stands in — see `detail`).
        available = self.has_live_integration and not settings.is_mock and configured
        return ProviderStatus(
            name=self.name,
            kind=self.kind,
            mode=settings.titan_mode,
            configured=configured,
            available=available,
            verified=available,
            detail=self._detail(settings, configured),
            capabilities=list(self.capabilities),
        )

    def _detail(self, settings: Settings, configured: bool) -> str:
        if not self.has_live_integration:
            return "No live integration in this build."
        if settings.is_mock:
            return self.mock_detail
        return self.live_detail(settings) if configured else self.missing_detail

    def live_detail(self, settings: Settings) -> str:
        return "Configured — live calls enabled."


class AgnesProvider(BaseProvider):
    """Agnes 3.0 Flash LLM slot (fact extraction / campaign brain)."""

    name = "agnes_llm"
    kind = "llm"
    capabilities = ("fact_extraction", "fact_validation", "campaign_director", "copy", "localization")
    mock_detail = "Mock mode: deterministic, labelled extraction and campaign templates."
    missing_detail = "Not configured: set TITAN_AGNES_API_BASE and TITAN_AGNES_API_KEY."

    def live_detail(self, settings: Settings) -> str:
        return f"Agnes text model {settings.agnes_text_model} — live calls enabled."

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.agnes_api_base and settings.agnes_api_key)



class AgnesImageProvider(BaseProvider):
    name = "agnes_image"
    kind = "image"
    capabilities = ("poster_art",)
    mock_detail = "Mock mode: posters use labelled fallback art (no image model)."
    missing_detail = "Not configured: set TITAN_AGNES_API_BASE and TITAN_AGNES_API_KEY."

    def live_detail(self, settings: Settings) -> str:
        return f"Agnes image model {settings.agnes_image_model} — live calls enabled."

    def is_configured(self, settings: Settings) -> bool:
        return bool(settings.agnes_api_base and settings.agnes_api_key)



class AgnesVideoProvider(BaseProvider):
    name = "agnes_video"
    kind = "video"
    capabilities = ("short_video",)
    mock_detail = "Mock mode: video jobs run against a labelled mock provider."
    missing_detail = "Not available: set the Agnes key and TITAN_ENABLE_AGNES_VIDEO=true."

    def live_detail(self, settings: Settings) -> str:
        if settings.video_engine == "magichour":
            return (
                f"Magic Hour text-to-video (model {settings.magichour_model}, "
                f"{len(settings.magichour_api_keys)} key(s)) — live async jobs enabled."
            )
        return f"Agnes video model {settings.agnes_video_model} — live async jobs enabled."

    def is_configured(self, settings: Settings) -> bool:
        if not settings.enable_agnes_video:
            return False
        if settings.video_engine == "magichour":
            return settings.magichour_configured
        return bool(settings.agnes_api_base and settings.agnes_api_key)



class VoiceProviderSlot(BaseProvider):
    name = "voice"
    kind = "voice"
    capabilities = ("tts", "voice_clone")
    mock_detail = "Mock mode: speech is a labelled test tone (no real voice)."
    missing_detail = "Not configured: set TITAN_SARVAM_API_KEY."

    def live_detail(self, settings: Settings) -> str:
        return (
            f"Sarvam Bulbul text-to-speech ({len(settings.sarvam_api_keys)} key(s)) "
            "— live calls enabled."
        )

    def is_configured(self, settings: Settings) -> bool:
        return settings.sarvam_configured



class PublisherProviderSlot(BaseProvider):
    name = "publisher"
    kind = "publishing"
    capabilities = ("sandbox", "wa_me", "agent_reach")
    has_live_integration = False


class ProviderGateway:
    """Registry + status reporter for provider slots."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings
        self._providers: dict[str, BaseProvider] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    @property
    def settings(self) -> Settings:
        return self._settings or get_settings()

    def register(self, provider: BaseProvider) -> BaseProvider:
        self._providers[provider.name] = provider
        self._breakers.setdefault(provider.name, CircuitBreaker())
        return provider

    def get(self, name: str) -> BaseProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ProviderUnavailableError(f"Unknown provider '{name}'.") from exc

    def breaker(self, name: str) -> CircuitBreaker:
        return self._breakers.setdefault(name, CircuitBreaker())

    def status_report(self) -> list[ProviderStatus]:
        settings = self.settings
        return [p.status(settings) for p in self._providers.values()]

    async def call_unverified(self, name: str) -> None:
        """Placeholder for provider-specific operations.

        Deliberately not implemented: we do not guess undocumented APIs.
        """
        provider = self.get(name)
        raise ProviderUnavailableError(
            f"Provider '{provider.name}' has no generic call interface; use its service module.",
            details={"provider": provider.name},
        )


def build_gateway(settings: Settings | None = None) -> ProviderGateway:
    gateway = ProviderGateway(settings)
    for provider in (
        AgnesProvider(),
        AgnesImageProvider(),
        AgnesVideoProvider(),
        VoiceProviderSlot(),
        PublisherProviderSlot(),
    ):
        gateway.register(provider)
    return gateway
