"""Windsor.ai MCP publisher (Phase 8, handoff §6).

Organic publishing ONLY, through a hosted MCP client that dynamically
discovers tools/actions on the connected account:

- transport: Streamable HTTP ``POST {mcp_url}`` (JSON-RPC 2.0 / MCP protocol);
- auth: bearer API key where supported (``TITAN_WINDSOR_API_KEY``); OAuth is
  the browser-driven alternative handled at setup-time;
- discovery: ``get_current_user`` → ``get_connectors`` → ``list_actions``;
- execution: ``execute_action`` AFTER explicit owner approval bound to asset
  hashes + caption hash + destination + action id/schema.

Hard rules from the handoff:
- read/analytics connectors are never presented as write capability;
- unsupported platform/media types produce a prepared manual/export package;
- nothing is reported PUBLISHED unless a successful action response is
  received and persisted;
- the mock registry keeps social endpoints OFF in ``TITAN_MODE=mock``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..errors import ConflictError, ProviderUnavailableError, ValidationError
from .checksums import sha256_text

PUBLISHER_STATES = (
    "NOT_CONNECTED",
    "ACTION_UNAVAILABLE",
    "READY_FOR_REVIEW",
    "APPROVED",
    "QUEUED",
    "PUBLISHING",
    "PUBLISHED",
    "FAILED",
    "MANUAL_REQUIRED",
    "CANCELLED",
)

KNOWN_TOOLS = (
    "get_current_user",
    "get_connectors",
    "get_connector_connect_info",
    "get_connector_authorization_url",
    "list_actions",
    "execute_action",
)

@dataclass
class DiscoveredAction:
    action_id: str
    platform: str
    media_kind: str  # image | video | text
    description: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "platform": self.platform,
            "media_kind": self.media_kind,
            "description": self.description,
            "schema": dict(self.schema),
        }


class WindsorMCPClient:
    """Minimal real MCP client over Streamable HTTP (JSON-RPC 2.0)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._settings.windsor_api_key:
            headers["Authorization"] = f"Bearer {self._settings.windsor_api_key}"
        return headers

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """One synchronous MCP tools/call (never used in mock mode)."""
        url = self._settings.windsor_mcp_url
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        try:
            response = httpx.post(url, headers=self._headers(), json=payload, timeout=30.0)
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(
                f"Windsor MCP unreachable: {exc}", code="windsor_unreachable"
            ) from exc
        if response.status_code in (401, 403):
            raise ProviderUnavailableError(
                "Windsor MCP rejected the credentials.", code="windsor_auth_failed"
            )
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"Windsor MCP error ({response.status_code}).", code="windsor_error"
            )
        body = response.json()
        return body.get("result", body)


class MockWindsorMCPClient:
    """Deterministic offline registry: organic-only, clearly mock."""

    name = "mock_windsor"

    ACTIONS = [
        DiscoveredAction(
            action_id="create_instagram_image_post_organic",
            platform="instagram",
            media_kind="image",
            description="Create an organic Instagram image post (mock).",
            schema={
                "type": "object",
                "properties": {
                    "image_url": {"type": "string"},
                    "caption": {"type": "string", "maxLength": 2200},
                },
                "required": ["image_url", "caption"],
            },
        ),
        DiscoveredAction(
            action_id="create_instagram_comment",
            platform="instagram",
            media_kind="text",
            description="Post an organic comment (mock).",
            schema={"type": "object", "properties": {"comment": {"type": "string"}}},
        ),
        # NOTE: Facebook/X appear as *connectors*, but per documented capability
        # limits no organic write action exists → they surface as MANUAL_REQUIRED.
    ]

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        if name == "get_current_user":
            return {"user": {"id": "mock-user", "email": "demo@example.invalid"}}
        if name == "get_connectors":
            return {
                "connectors": [
                    {"id": "instagram", "name": "Instagram", "connected": True},
                    {"id": "facebook", "name": "Facebook", "connected": True},
                    {"id": "x", "name": "X", "connected": True},
                ]
            }
        if name == "list_actions":
            return {
                "actions": [
                    {
                        "id": a.action_id,
                        "description": a.description,
                        "inputSchema": a.schema,
                    }
                    for a in self.ACTIONS
                ]
            }
        if name == "execute_action":
            arguments = arguments or {}
            action_id = arguments.get("action_id", "")
            known = {a.action_id for a in self.ACTIONS}
            if action_id not in known:
                return {"isError": True, "error": f"Unknown action {action_id}"}
            return {
                "result": {"ok": True, "post_id": f"mock-post-{hashlib.sha1(json.dumps(arguments, sort_keys=True).encode()).hexdigest()[:12]}"},
            }
        return {"isError": True, "error": f"Unknown tool {name}"}


def publisher_states_for(platform: str, media_kind: str, actions: list[DiscoveredAction]) -> tuple[str, DiscoveredAction | None]:
    match = [
        a
        for a in actions
        if a.platform == platform and a.media_kind == media_kind
    ]
    if match:
        return "READY_FOR_REVIEW", match[0]
    return "MANUAL_REQUIRED", None


def validate_payload(payload: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    """Validate a prepared payload against the discovered action schema (subset)."""
    issues: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    for key in schema.get("required", []):
        if payload.get(key) in (None, ""):
            issues.append(f"missing required field: {key}")
    props = schema.get("properties", {})
    for key, value in payload.items():
        spec = props.get(key)
        if not spec:
            issues.append(f"unknown field for action: {key}")
            continue
        expected = spec.get("type")
        if expected == "string" and not isinstance(value, str):
            issues.append(f"field {key} must be a string")
        max_len = spec.get("maxLength")
        if max_len and isinstance(value, str) and len(value) > max_len:
            issues.append(f"field {key} exceeds maxLength {max_len}")
    return issues


def caption_hash(caption: str) -> str:
    """Approval binding for the exact caption text (shared checksum format)."""
    return sha256_text(caption)


# ── approval-gated publication ────────────────────────────────────────
@dataclass
class PublishPreparation:
    platform: str
    media_kind: str
    action: DiscoveredAction | None
    status: str
    payload: dict[str, Any] | None = None
    manual_package: dict[str, Any] | None = None
    validation_issues: list[str] = field(default_factory=list)


def prepare_publication(
    *,
    platform: str,
    media_kind: str,
    caption: str,
    asset_url: str | None,
    actions: list[DiscoveredAction],
) -> PublishPreparation:
    if media_kind not in ("image", "video", "text"):
        raise ValidationError(f"Unsupported media kind {media_kind!r}.", code="media_unsupported")
    if platform == "whatsapp" and media_kind == "text":
        # Handoff §6: wa.me always allowed — the user reviews and sends it;
        # this is NOT unattended WhatsApp broadcasting.
        return PublishPreparation(
            platform=platform,
            media_kind=media_kind,
            action=None,
            status="READY_FOR_REVIEW",
            payload={"text": caption},
        )
    status, action = publisher_states_for(platform, media_kind, actions)
    if status == "MANUAL_REQUIRED" or action is None:
        package = {
            "platform": platform,
            "media_kind": media_kind,
            "caption": caption,
            "caption_hash": caption_hash(caption),
            "asset_url": asset_url,
            "instructions": f"Post manually to {platform}; Titan will not fake a success.",
        }
        return PublishPreparation(
            platform=platform, media_kind=media_kind, action=None, status="MANUAL_REQUIRED",
            manual_package=package,
        )
    payload: dict[str, Any] = {}
    if media_kind == "image":
        payload = {"image_url": asset_url or "", "caption": caption}
    elif media_kind == "text":
        payload = {"comment": caption}
    elif media_kind == "video":
        payload = {"video_url": asset_url or "", "caption": caption}
    validation_issues = validate_payload(payload, action.schema)
    if validation_issues:
        return PublishPreparation(
            platform=platform, media_kind=media_kind, action=action,
            status="ACTION_UNAVAILABLE", payload=payload,
            validation_issues=validation_issues,
        )
    return PublishPreparation(
        platform=platform, media_kind=media_kind, action=action,
        status="READY_FOR_REVIEW", payload=payload,
    )


def execute_publication(
    preparation: PublishPreparation,
    *,
    approval_id: int,
    client: Any,
) -> dict[str, Any]:
    """Execute an APPROVED publication. Returns the raw tool response.

    A tool error (response ``isError``) is returned verbatim — the caller must
    persist it and NEVER report PUBLISHED from a mere invocation attempt.
    """
    if preparation.status != "READY_FOR_REVIEW" or preparation.action is None:
        raise ConflictError(
            "Publication is not ready (unsupported or validation failed).",
            code="publish_not_ready",
        )
    response = client.call_tool(
        "execute_action",
        {
            "action_id": preparation.action.action_id,
            "payload": preparation.payload,
            "approval_id": approval_id,
        },
    )
    return response


def wa_me_url(text: str) -> str:
    """Prepared WhatsApp deep link — the user reviews and sends it themselves."""
    import urllib.parse

    return f"https://wa.me/?text={urllib.parse.quote(text)}"


def get_mcp_client(settings: Settings | None = None) -> Any:
    """Mock client in mock mode (no social calls); real client in live mode."""
    settings = settings or get_settings()
    if settings.is_mock or not settings.windsor_configured:
        return MockWindsorMCPClient()
    return WindsorMCPClient(settings)


__all__ = [
    "PUBLISHER_STATES",
    "KNOWN_TOOLS",
    "DiscoveredAction",
    "WindsorMCPClient",
    "MockWindsorMCPClient",
    "publisher_states_for",
    "validate_payload",
    "caption_hash",
    "prepare_publication",
    "execute_publication",
    "wa_me_url",
    "get_mcp_client",
]
