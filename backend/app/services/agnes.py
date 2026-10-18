"""Agnes HTTP clients (real, network-capable).

Contract verified against https://wiki.agnes-ai.com (model guides for
agnes-3.0-flash, agnes-image-2.5-flash and agnes-video-2.5):

- LLM:   POST {base}/chat/completions  — OpenAI-compatible; the guide documents
         ``model``/``messages``/``temperature``/``top_p``/``max_tokens`` and no
         JSON mode, so JSON is requested in the prompt and parsed defensively.
- Image: POST {base}/images/generations — ``size`` is a tier (``1K``..``4K``),
         ``ratio`` an aspect ratio, and ``response_format`` lives under
         ``extra_body``; the image is read from ``data[0].url``/``b64_json``.
- Video: POST {base}/videos (async) then
         GET {host}/agnesapi?video_id=…&model_name=… until ``status`` is
         ``completed`` (top-level ``url``) or ``failed`` (``error.message``).

``TITAN_MODE=live`` with a configured key reaches these clients directly. A
missing key is a visible configuration error, never a silent mock. In
``TITAN_MODE=mock`` the mock providers below are used instead and are always
explicitly labelled ``is_mock=True``.
"""

from __future__ import annotations

import abc
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..errors import ProviderUnavailableError, ValidationError
from .gateway import CircuitBreaker, ProviderError, with_retry

#: Retained so older .env files keep loading; it no longer gates live calls
#: (live mode + a configured key is the gate).
VERIFY_KEY = "TITAN_AGNES_INTERFACE_VERIFIED"

#: Percent-word/format helpers shared with the mock plan generator.
TARGETED_LANGUAGES = ("English", "Hindi", "Kannada", "Tamil", "Telugu")
LOCALE_CODE_MAP = {
    "English": "en-IN",
    "Hindi": "hi-IN",
    "Kannada": "kn-IN",
    "Tamil": "ta-IN",
    "Telugu": "te-IN",
}
REGION_FOR_LANGUAGE = {
    "English": "Pan-India",
    "Hindi": "Delhi",
    "Kannada": "Bengaluru",
    "Tamil": "Chennai",
    "Telugu": "Hyderabad",
}

#: Documented Agnes video task states; the last two are terminal.
VIDEO_STATUS_QUEUED = "queued"
VIDEO_STATUS_IN_PROGRESS = "in_progress"
VIDEO_STATUS_COMPLETED = "completed"
VIDEO_STATUS_FAILED = "failed"
VIDEO_STATUSES = (
    VIDEO_STATUS_QUEUED,
    VIDEO_STATUS_IN_PROGRESS,
    VIDEO_STATUS_COMPLETED,
    VIDEO_STATUS_FAILED,
)


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    is_mock: bool
    request_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class ImageResult:
    image_url: str | None
    image_b64: str | None
    provider: str
    model: str
    is_mock: bool
    request_id: str | None = None


@dataclass
class VideoTask:
    """One snapshot of an Agnes video task."""

    video_id: str
    status: str  # one of VIDEO_STATUSES
    progress: int | None
    url: str | None
    error: str | None
    provider: str
    model: str
    is_mock: bool


# ── shared HTTP helpers ────────────────────────────────────────────────
def _auth_headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.agnes_api_key}"}


def _provider_error_for(response: httpx.Response, provider: str) -> ProviderError | None:
    """Map an Agnes HTTP status to a normalized error (None when 2xx).

    Permanent failures (auth, quota, validation) are not retryable; rate limits
    and gateway/server errors are.
    """
    status = response.status_code
    if status < 400:
        return None
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        elif body.get("message"):
            detail = str(body["message"])
    suffix = f": {detail[:300]}" if detail else "."

    # Agnes reports an exhausted balance as 403 "Insufficient user quota".
    lowered = detail.lower()
    if status == 402 or (status == 403 and ("quota" in lowered or "balance" in lowered)):
        return ProviderError(
            f"The Agnes account has insufficient balance or quota for this model{suffix}",
            provider=provider, code="provider_quota_exceeded", retryable=False, status_code=status,
        )
    if status in (401, 403):
        return ProviderError(
            f"Agnes rejected the API key or model access ({status}){suffix}",
            provider=provider, code="provider_auth_error", retryable=False, status_code=status,
        )
    if status == 429:
        return ProviderError(
            "Agnes rate limit reached. Try again in a minute.",
            provider=provider, code="provider_rate_limited", retryable=True, status_code=status,
        )
    if status in (408, 499) or status >= 500:
        return ProviderError(
            f"Agnes server error ({status}){suffix}",
            provider=provider, code="provider_server_error", retryable=True, status_code=status,
        )
    return ProviderError(
        f"Agnes rejected the request ({status}){suffix}",
        provider=provider, code="provider_bad_request", retryable=False, status_code=status,
    )


def _json_body(response: httpx.Response, provider: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderError(
            "Agnes returned a non-JSON response.",
            provider=provider, code="provider_bad_response", retryable=False,
        ) from exc
    if not isinstance(body, dict):
        raise ProviderError(
            "Agnes returned an unexpected response shape.",
            provider=provider, code="provider_bad_response", retryable=False,
        )
    return body


# ── LLM ────────────────────────────────────────────────────────────────
class LLMClient(abc.ABC):
    name: str = "llm"

    @abc.abstractmethod
    async def complete_json(
        self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.4
    ) -> LLMResult:
        ...


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model response (tolerates code fences)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ProviderUnavailableError(
            "Agnes LLM returned a non-JSON response.",
            code="llm_bad_output",
        )
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ProviderUnavailableError(
            "Agnes LLM returned invalid JSON.",
            code="llm_bad_json",
            details={"error": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderUnavailableError(
            "Agnes LLM JSON output was not an object.",
            code="llm_bad_output",
        )
    return parsed


#: One breaker per process so repeated failures actually open the circuit.
_LLM_BREAKER = CircuitBreaker()


class AgnesLLMClient(LLMClient):
    """Real chat-completions client for the configured Agnes text model."""

    name = "agnes"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def complete_json(
        self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.4
    ) -> LLMResult:
        settings = self._settings
        url = settings.agnes_api_base.rstrip("/") + "/chat/completions"
        # No `response_format`: the Agnes 3.0 Flash guide documents no JSON
        # mode, so the JSON contract is carried by the prompt and enforced by
        # the caller's schema validation.
        payload = {
            "model": settings.agnes_text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        timeout = settings.provider_timeout

        async def _call() -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(url, headers=_auth_headers(settings), json=payload)
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Agnes request failed ({type(exc).__name__}).",
                    provider=self.name,
                    code="provider_network_error",
                    retryable=True,
                ) from exc
            error = _provider_error_for(response, self.name)
            if error is not None:
                raise error
            return _json_body(response, self.name)

        try:
            body = await with_retry(
                _call,
                provider=self.name,
                max_retries=settings.provider_max_retries,
                backoff_base=settings.provider_backoff_base,
                timeout=timeout + 5.0,
                breaker=_LLM_BREAKER,
            )
        except ProviderError as exc:
            raise ProviderUnavailableError(
                exc.message, code=exc.code, details=exc.as_dict()
            ) from exc

        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderUnavailableError(
                "Agnes response did not contain a message content field.",
                code="llm_bad_response",
                details={"keys": list(body.keys())},
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderUnavailableError(
                "Agnes returned an empty completion.", code="llm_bad_response"
            )
        usage = body.get("usage")
        request_id = body.get("id")
        return LLMResult(
            text=content,
            provider=self.name,
            model=settings.agnes_text_model,
            is_mock=False,
            request_id=request_id if isinstance(request_id, str) else None,
            usage=usage if isinstance(usage, dict) else {},
        )


class MockLLMClient(LLMClient):
    """Deterministic offline LLM. Produces clearly-labelled templated JSON.

    It never fabricates fact VALUES: callers must pass a token map, and every
    template is validated by ``substitute_tokens`` afterwards. This keeps the
    mock path truthful — copy contains only tokens and safe framing words.
    """

    name = "mock_llm"

    @staticmethod
    def _tokens_line(tokens: dict[str, str]) -> str:
        return ", ".join(f"{k}={{{{{k}}}}}" for k in sorted(tokens))

    async def complete_json(
        self, system: str, user: str, *, max_tokens: int = 2000, temperature: float = 0.4
    ) -> LLMResult:
        # The mock client echoes the caller's instruction envelope so the
        # campaign brain can build deterministic plans from it.
        try:
            envelope = json.loads(user)
        except json.JSONDecodeError:
            envelope = {}
        if not isinstance(envelope, dict):
            envelope = {}
        return LLMResult(
            text=json.dumps(envelope, ensure_ascii=False),
            provider=self.name,
            model="mock-template-engine",
            is_mock=True,
        )


# ── image ──────────────────────────────────────────────────────────────
class ImageClient(abc.ABC):
    name: str = "image"

    @abc.abstractmethod
    async def generate_art(self, prompt: str, *, size: str = "1K", ratio: str = "3:4") -> ImageResult:
        ...


class AgnesImageClient(ImageClient):
    """Real Agnes Image 2.5 Flash client (URL response format)."""

    name = "agnes_image"

    #: The image guide recommends a 60–360 s client timeout.
    TIMEOUT = 180.0

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def generate_art(self, prompt: str, *, size: str = "1K", ratio: str = "3:4") -> ImageResult:
        settings = self._settings
        url = settings.agnes_api_base.rstrip("/") + "/images/generations"
        # Documented: `size` is a tier, `ratio` is separate, and
        # `response_format` lives under `extra_body` (top-level is rejected).
        payload = {
            "model": settings.agnes_image_model,
            "prompt": prompt,
            "size": size,
            "ratio": ratio,
            "extra_body": {"response_format": "url"},
        }

        async def _call() -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                    response = await client.post(url, headers=_auth_headers(settings), json=payload)
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Agnes Image request failed ({type(exc).__name__}).",
                    provider=self.name, code="provider_network_error", retryable=True,
                ) from exc
            error = _provider_error_for(response, self.name)
            if error is not None:
                raise error
            return _json_body(response, self.name)

        try:
            body = await with_retry(
                _call, provider=self.name, max_retries=1, backoff_base=1.0, timeout=self.TIMEOUT + 5.0
            )
        except ProviderError as exc:
            raise ProviderUnavailableError(
                exc.message, code=exc.code, details=exc.as_dict()
            ) from exc

        items = body.get("data")
        data = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        url_out = data.get("url")
        b64 = data.get("b64_json")
        if not url_out and not b64:
            raise ProviderUnavailableError(
                "Agnes Image response contained no image.",
                code="image_bad_response",
            )
        return ImageResult(
            image_url=url_out,
            image_b64=b64,
            provider=self.name,
            model=settings.agnes_image_model,
            is_mock=False,
        )


# ── video ──────────────────────────────────────────────────────────────
class VideoClient(abc.ABC):
    name: str = "video"

    @abc.abstractmethod
    async def create(self, prompt: str, *, seconds: int, size: str, aspect_ratio: str) -> VideoTask:
        ...

    @abc.abstractmethod
    async def retrieve(self, video_id: str) -> VideoTask:
        ...

    @abc.abstractmethod
    async def download(self, url: str, *, max_bytes: int) -> bytes:
        ...


def _video_task_from(
    body: dict[str, Any], *, fallback_id: str | None, provider: str, model: str
) -> VideoTask:
    video_id = body.get("video_id") or body.get("id") or fallback_id
    if not video_id or not isinstance(video_id, str):
        raise ProviderUnavailableError(
            "Agnes Video response contained no video id.", code="video_bad_response"
        )
    status = str(body.get("status") or "").lower()
    if status not in VIDEO_STATUSES:
        raise ProviderUnavailableError(
            f"Agnes Video returned an unknown status {status!r}.",
            code="video_bad_response",
        )
    progress = body.get("progress")
    error = body.get("error")
    message: str | None = None
    if isinstance(error, dict):
        message = str(error.get("message") or "") or None
    elif isinstance(error, str):
        message = error or None
    url = body.get("url")
    return VideoTask(
        video_id=video_id,
        status=status,
        progress=int(progress) if isinstance(progress, (int, float)) else None,
        url=url if isinstance(url, str) and url.startswith(("http://", "https://")) else None,
        error=message,
        provider=provider,
        model=model,
        is_mock=False,
    )


class AgnesVideoClient(VideoClient):
    """Agnes Video 2.5 — asynchronous text-to-video tasks."""

    name = "agnes_video"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _retrieve_url(self) -> str:
        """``{host}/agnesapi`` — the documented task endpoint sits beside ``/v1``."""
        base = self._settings.agnes_api_base.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        return base + "/agnesapi"

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        settings = self._settings
        timeout = settings.provider_timeout

        async def _call() -> dict[str, Any]:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.request(
                        method, url, headers=_auth_headers(settings), **kwargs
                    )
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"Agnes Video request failed ({type(exc).__name__}).",
                    provider=self.name, code="provider_network_error", retryable=True,
                ) from exc
            error = _provider_error_for(response, self.name)
            if error is not None:
                if response.status_code == 404:
                    error.code = "video_not_found"
                raise error
            return _json_body(response, self.name)

        try:
            # A create is not idempotent (every POST bills a new task), so only
            # the read-only poll is retried.
            return await with_retry(
                _call,
                provider=self.name,
                max_retries=0 if method == "POST" else settings.provider_max_retries,
                backoff_base=settings.provider_backoff_base,
                timeout=timeout + 5.0,
            )
        except ProviderError as exc:
            raise ProviderUnavailableError(
                exc.message, code=exc.code, details=exc.as_dict()
            ) from exc

    async def create(self, prompt: str, *, seconds: int, size: str, aspect_ratio: str) -> VideoTask:
        settings = self._settings
        body = await self._request(
            "POST",
            settings.agnes_api_base.rstrip("/") + "/videos",
            json={
                "model": settings.agnes_video_model,
                "prompt": prompt,
                "mode": "text",
                "seconds": str(seconds),  # documented as a string, "4".."12"
                "size": size,
                "aspect_ratio": aspect_ratio,
            },
        )
        return _video_task_from(
            body, fallback_id=None, provider=self.name, model=settings.agnes_video_model
        )

    async def retrieve(self, video_id: str) -> VideoTask:
        settings = self._settings
        body = await self._request(
            "GET",
            self._retrieve_url(),
            params={"video_id": video_id, "model_name": settings.agnes_video_model},
        )
        return _video_task_from(
            body, fallback_id=video_id, provider=self.name, model=settings.agnes_video_model
        )

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        try:
            async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code >= 400:
                        raise ProviderUnavailableError(
                            f"The generated video could not be downloaded ({response.status_code}).",
                            code="video_download_failed",
                        )
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ProviderUnavailableError(
                                "The generated video exceeds the size limit.",
                                code="video_too_large",
                            )
                        chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"The generated video could not be downloaded ({type(exc).__name__}).",
                code="video_download_failed",
            ) from exc
        return b"".join(chunks)


def _box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


#: A structurally valid but frame-less MP4 container. Used ONLY by the mock
#: video provider, always labelled ``is_mock`` — it is not playable footage.
MOCK_MP4 = (
    _box(b"ftyp", b"isom" + (512).to_bytes(4, "big") + b"isomiso2mp41")
    + _box(b"free", b"titan-mock-video")
    + _box(b"mdat", b"")
)


class MockVideoClient(VideoClient):
    """Deterministic offline video provider.

    Mirrors the real task lifecycle (queued → in_progress → completed) so the
    job machinery and the UI are exercised end to end, without impersonating
    Agnes: every task is ``is_mock=True`` and the "video" is an empty container.
    """

    name = "mock_video"
    MODEL = "mock-video-engine"

    def _task(self, video_id: str, status: str, progress: int, url: str | None = None) -> VideoTask:
        return VideoTask(
            video_id=video_id, status=status, progress=progress, url=url, error=None,
            provider=self.name, model=self.MODEL, is_mock=True,
        )

    async def create(self, prompt: str, *, seconds: int, size: str, aspect_ratio: str) -> VideoTask:
        digest = hashlib.sha256(
            f"{prompt}|{seconds}|{size}|{aspect_ratio}".encode("utf-8")
        ).hexdigest()[:16]
        return self._task(f"mock-video-{digest}-0", VIDEO_STATUS_QUEUED, 0)

    async def retrieve(self, video_id: str) -> VideoTask:
        # The poll count rides in the id suffix so the mock stays stateless.
        head, _, tail = video_id.rpartition("-")
        polls = int(tail) + 1 if tail.isdigit() else 1
        next_id = f"{head}-{polls}"
        if polls < 2:
            return self._task(next_id, VIDEO_STATUS_IN_PROGRESS, 50)
        return self._task(next_id, VIDEO_STATUS_COMPLETED, 100, url="mock://video")

    async def download(self, url: str, *, max_bytes: int) -> bytes:
        return MOCK_MP4


def parse_llm_json(result: LLMResult) -> dict[str, Any]:
    """Validate + parse an LLMResult into a JSON object."""
    if result.is_mock:
        # Mock clients emit JSON directly.
        try:
            return json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise ValidationError("Mock plan output was not JSON.", code="mock_bad_output") from exc
    return _extract_json_object(result.text)


# ── factories ──────────────────────────────────────────────────────────
def agnes_live_allowed(settings: Settings) -> bool:
    """Live Agnes calls need live mode and a configured endpoint + key."""
    return bool(not settings.is_mock and settings.agnes_configured)


#: Older call sites import the private name.
_live_llm_allowed = agnes_live_allowed

_NOT_CONFIGURED = (
    "Agnes is not configured: set TITAN_AGNES_API_BASE and TITAN_AGNES_API_KEY "
    "(or run with TITAN_MODE=mock)."
)


def _require_configured(settings: Settings) -> None:
    if not settings.agnes_configured:
        raise ProviderUnavailableError(_NOT_CONFIGURED, code="provider_not_configured")


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    settings = settings or get_settings()
    if settings.is_mock:
        return MockLLMClient()
    _require_configured(settings)
    return AgnesLLMClient(settings)


def get_image_client(settings: Settings | None = None) -> ImageClient:
    """Live image client. Mock mode has no image model (fallback art is used)."""
    settings = settings or get_settings()
    if settings.is_mock:
        raise ProviderUnavailableError(
            "Image generation is not available in mock mode.", code="provider_mock_mode"
        )
    _require_configured(settings)
    return AgnesImageClient(settings)


def get_video_client(settings: Settings | None = None) -> VideoClient:
    settings = settings or get_settings()
    if not settings.enable_agnes_video:
        raise ProviderUnavailableError(
            "AI video generation is disabled (TITAN_ENABLE_AGNES_VIDEO=false).",
            code="video_disabled",
        )
    if settings.is_mock:
        return MockVideoClient()
    if settings.video_engine == "magichour":
        from .magichour import MagicHourVideoClient

        if not settings.magichour_configured:
            raise ProviderUnavailableError(
                "Magic Hour is not configured: set TITAN_MAGICHOUR_API_KEYS.",
                code="provider_not_configured",
            )
        return MagicHourVideoClient(settings)
    _require_configured(settings)
    return AgnesVideoClient(settings)


__all__ = [
    "LLMClient",
    "LLMResult",
    "ImageClient",
    "ImageResult",
    "VideoClient",
    "VideoTask",
    "AgnesLLMClient",
    "MockLLMClient",
    "AgnesImageClient",
    "AgnesVideoClient",
    "MockVideoClient",
    "MOCK_MP4",
    "VIDEO_STATUSES",
    "VIDEO_STATUS_QUEUED",
    "VIDEO_STATUS_IN_PROGRESS",
    "VIDEO_STATUS_COMPLETED",
    "VIDEO_STATUS_FAILED",
    "agnes_live_allowed",
    "get_llm_client",
    "get_image_client",
    "get_video_client",
    "parse_llm_json",
    "TARGETED_LANGUAGES",
    "LOCALE_CODE_MAP",
    "REGION_FOR_LANGUAGE",
    "VERIFY_KEY",
]
