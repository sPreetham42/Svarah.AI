"""Request/response schemas for the Phase 1 API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .services.fact_schemas import FactSheet

MAX_TEXT_LENGTH = 20_000


class CampaignCreate(BaseModel):
    """POST /api/campaigns input (Source of Truth §46).

    Exactly one of `text` / `audio_b64` must carry content.
    """

    shop_id: int | None = Field(default=None, ge=1)
    text: str | None = Field(default=None, max_length=MAX_TEXT_LENGTH)
    audio_b64: str | None = None
    audio_mime: str | None = Field(default=None, max_length=128)
    language_hint: str | None = Field(default=None, max_length=16)

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _require_content(self) -> "CampaignCreate":
        if not self.text and not self.audio_b64:
            raise ValueError("Provide either 'text' or 'audio_b64'.")
        return self


class STTStatus(BaseModel):
    status: str  # "ok" | "unavailable" | "error" | "skipped"
    provider: str | None = None
    code: str | None = None
    message: str | None = None
    fallback: str | None = None


class TranscriptView(BaseModel):
    raw: str
    normalized: str | None = None
    hash: str | None = None
    language: str | None = None
    duration_seconds: float | None = None
    confidence: float | None = None
    provider: str | None = None
    is_mock: bool = False
    segments: list[dict[str, Any]] = Field(default_factory=list)


class AuditEventView(BaseModel):
    id: int
    event_type: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ExtractionStatus(BaseModel):
    status: str  # "ok" | "unavailable" | "error"
    provider: str | None = None
    is_mock: bool = False
    message: str | None = None
    fallback: str | None = None


class FactSheetVersionSummary(BaseModel):
    id: int
    version: int
    status: str
    fact_hash: str | None = None
    created_at: datetime
    locked_at: datetime | None = None


class FactSheetRead(BaseModel):
    id: int
    campaign_id: int
    version: int
    status: str  # draft | locked | superseded
    facts: FactSheet
    tokens: dict[str, str] | None = None
    fact_hash: str | None = None
    seal: str | None = None
    seal_algorithm: str | None = None
    #: Recomputed seal check; null when no seal secret is configured.
    seal_valid: bool | None = None
    extraction: ExtractionStatus | None = None
    #: Deterministic + Agnes semantic validation (see services/fact_validation).
    #: `stale` is true when the facts changed after it was computed.
    validation: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    locked_at: datetime | None = None


class CampaignRead(BaseModel):
    id: int
    shop_id: int
    status: str
    input_type: str | None = None
    transcript: TranscriptView | None = None
    audio_path: str | None = None
    facts: FactSheet | None = None
    factsheet: FactSheetRead | None = None
    factsheet_versions: list[FactSheetVersionSummary] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    verification: list[dict[str, Any]] = Field(default_factory=list)
    audit_events: list[AuditEventView] = Field(default_factory=list)
    stt: STTStatus | None = None
    created_at: datetime
    updated_at: datetime


class CampaignSummary(BaseModel):
    id: int
    shop_id: int
    status: str
    input_type: str | None = None
    has_transcript: bool
    created_at: datetime
    updated_at: datetime


class TypedTranscriptIn(BaseModel):
    """POST /api/campaigns/{id}/transcript — typed recovery after STT failure."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_LENGTH)
    language_hint: str | None = Field(default=None, max_length=16)

    @field_validator("text")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        return value.strip()


class HealthResponse(BaseModel):
    status: str
    version: str
    mode: str
    database: str
    assets_dir: str
    time: datetime


class ProviderStatusView(BaseModel):
    name: str
    kind: str
    mode: str
    configured: bool
    available: bool
    verified: bool
    detail: str
    capabilities: list[str] = Field(default_factory=list)


class ModesResponse(BaseModel):
    mode: str
    providers: list[ProviderStatusView]
    stt: ProviderStatusView
    extraction: ProviderStatusView
    seal: dict[str, Any]
    #: Authentication configuration, never the secret itself.
    auth: dict[str, Any]
    storage: dict[str, Any]
    database: dict[str, Any]
