"""Magic Hour text-to-video client (https://docs.magichour.ai).

Interface used:
    POST {base}/v1/text-to-video        → {"id", "credits_charged"}
        body: end_seconds, aspect_ratio, style.prompt, model?, resolution?
    GET  {base}/v1/video-projects/{id}  → status queued|rendering|complete|
        error|canceled, downloads[{url, expires_at}], error{code, message}

Several API keys may be configured. A create call walks them in order and
moves on when a key is rejected (401) or out of credits (402); the key that
accepted the task is recorded in the task id (``mh<index>:<id>``) because a
project can only be read back with the key that created it. Nothing here falls
back to another provider or to a mock: when every key is refused, the last
refusal is raised.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..config import Settings
from ..errors import ProviderUnavailableError
from .agnes import (
    VIDEO_STATUS_COMPLETED,
    VIDEO_STATUS_FAILED,
    VIDEO_STATUS_IN_PROGRESS,
    VIDEO_STATUS_QUEUED,
    AgnesVideoClient,
    VideoTask,
)

_STATUS_MAP = {
    "draft": VIDEO_STATUS_QUEUED,
    "queued": VIDEO_STATUS_QUEUED,
    "rendering": VIDEO_STATUS_IN_PROGRESS,
    "complete": VIDEO_STATUS_COMPLETED,
    "error": VIDEO_STATUS_FAILED,
    "canceled": VIDEO_STATUS_FAILED,
}
_ASPECT_RATIOS = ("9:16", "16:9", "1:1")
#: A refusal that is about this key, not the request — try the next key.
_KEY_SCOPED = (401, 402)


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("code") or "")[:300]
    return ""


def _error_for(response: httpx.Response) -> ProviderUnavailableError:
    status = response.status_code
    detail = _message(response)
    if status == 401:
        code, text = "provider_auth_error", "Magic Hour rejected the API key."
    elif status == 402:
        code, text = "provider_quota_exceeded", "The Magic Hour account is out of credits or needs a plan."
    elif status == 404:
        code, text = "video_not_found", "Magic Hour does not know this video."
    elif status in (400, 422):
        code, text = "provider_bad_request", "Magic Hour rejected the video request."
    elif status == 429:
        code, text = "provider_rate_limited", "Magic Hour is rate limiting requests."
    else:
        code, text = "provider_unavailable", f"Magic Hour returned HTTP {status}."
    return ProviderUnavailableError(
        f"{text} {detail}".strip(), code=code, details={"provider": "magichour", "status": status}
    )


class MagicHourVideoClient(AgnesVideoClient):
    """Asynchronous text-to-video tasks. `download` is inherited (plain HTTPS)."""

    name = "magichour_video"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self._base = settings.magichour_api_base.rstrip("/")
        self._keys = settings.magichour_api_keys

    async def _call(self, method: str, path: str, key: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._settings.provider_timeout) as client:
                return await client.request(
                    method,
                    f"{self._base}{path}",
                    headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Magic Hour request failed ({type(exc).__name__}).",
                code="provider_network_error",
                details={"provider": "magichour"},
            ) from exc

    async def create(self, prompt: str, *, seconds: int, size: str, aspect_ratio: str) -> VideoTask:
        settings = self._settings
        body: dict[str, Any] = {
            "name": "Svarah campaign video",
            "end_seconds": seconds,
            "aspect_ratio": aspect_ratio if aspect_ratio in _ASPECT_RATIOS else "9:16",
            "style": {"prompt": prompt},
        }
        if settings.magichour_model and settings.magichour_model != "default":
            body["model"] = settings.magichour_model
        if settings.magichour_resolution:
            body["resolution"] = settings.magichour_resolution

        last: ProviderUnavailableError | None = None
        for index, key in enumerate(self._keys):
            response = await self._call("POST", "/v1/text-to-video", key, json=body)
            if response.status_code < 300:
                data = response.json() if response.content else {}
                project_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(project_id, str) or not project_id:
                    raise ProviderUnavailableError(
                        "Magic Hour accepted the request but returned no video id.",
                        code="video_bad_response",
                    )
                return VideoTask(
                    video_id=f"mh{index}:{project_id}",
                    status=VIDEO_STATUS_QUEUED,
                    progress=0,
                    url=None,
                    error=None,
                    provider=self.name,
                    model=settings.magichour_model,
                    is_mock=False,
                )
            last = _error_for(response)
            if response.status_code not in _KEY_SCOPED:
                raise last  # the request itself is the problem; other keys will not help
        raise last or ProviderUnavailableError(
            "Magic Hour is not configured: set TITAN_MAGICHOUR_API_KEYS.",
            code="provider_not_configured",
        )

    async def retrieve(self, video_id: str) -> VideoTask:
        head, _, project_id = video_id.partition(":")
        index = int(head[2:]) if head.startswith("mh") and head[2:].isdigit() else -1
        if not project_id or not 0 <= index < len(self._keys):
            raise ProviderUnavailableError(
                "This video was created with a Magic Hour key that is no longer configured.",
                code="video_not_found",
            )
        response = await self._call("GET", f"/v1/video-projects/{project_id}", self._keys[index])
        if response.status_code >= 300:
            raise _error_for(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderUnavailableError(
                "Magic Hour returned a response that is not JSON.", code="video_bad_response"
            ) from exc
        status = _STATUS_MAP.get(str(data.get("status") or "").lower())
        if status is None:
            raise ProviderUnavailableError(
                f"Magic Hour returned an unknown status {data.get('status')!r}.",
                code="video_bad_response",
            )
        url: str | None = None
        downloads = data.get("downloads")
        if isinstance(downloads, list) and downloads and isinstance(downloads[0], dict):
            candidate = downloads[0].get("url")
            if isinstance(candidate, str) and candidate.startswith("https://"):
                url = candidate
        error = data.get("error")
        message = None
        if isinstance(error, dict):
            message = str(error.get("message") or error.get("code") or "") or None
        if status == VIDEO_STATUS_FAILED and not message:
            message = f"Magic Hour reported the video as {data.get('status')}."
        return VideoTask(
            video_id=video_id,
            status=status,
            progress=50 if status == VIDEO_STATUS_IN_PROGRESS else None,
            url=url,
            error=message,
            provider=self.name,
            model=self._settings.magichour_model,
            is_mock=False,
        )
