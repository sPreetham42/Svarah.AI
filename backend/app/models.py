"""Initial SQLAlchemy models for Phase 1.

Scope: the four foundation tables explicitly required for this phase
(`shops`, `campaigns`, `jobs`, `audit_events`). Later-phase tables
(`fact_sheets`, `assets`, `verification_results`, `publish_records`,
`voice_profiles`) are intentionally deferred.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Status vocabularies (Source of Truth §36, §24) ────────────────────
class CampaignStatus:
    CAPTURED = "captured"
    EXTRACTED = "extracted"
    LOCKED = "locked"
    GENERATING = "generating"
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    APPROVED = "approved"
    PUBLISHED = "published"
    EXPORTED = "exported"

    ALL = (
        CAPTURED,
        EXTRACTED,
        LOCKED,
        GENERATING,
        VERIFIED,
        NEEDS_REVIEW,
        APPROVED,
        PUBLISHED,
        EXPORTED,
    )


class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    REPAIRING = "repairing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"

    ALL = (QUEUED, RUNNING, VALIDATING, REPAIRING, COMPLETED, FAILED, BLOCKED, CANCELLED)


class InputType:
    TYPED = "typed"
    AUDIO = "audio"

    ALL = (TYPED, AUDIO)


class FactSheetStatus:
    DRAFT = "draft"
    LOCKED = "locked"
    SUPERSEDED = "superseded"

    ALL = (DRAFT, LOCKED, SUPERSEDED)


class User(Base):
    """A registered account.

    ``owner_uid`` — not the row id — is the identifier every owned record
    references, so a database reset keeps existing tokens meaningful and no
    resource is ever addressed by a guessable sequential owner number.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_uid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Null for a demo account created by the mock-mode password-less sign-in.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: E.164 number for an account created by phone sign-in; null otherwise.
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Owning account. Nullable only so pre-authentication rows still load;
    #: a null owner is unreachable through the API (see `app.deps`).
    owner_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en")
    brand_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    campaigns: Mapped[list["Campaign"]] = relationship(back_populates="shop")


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    shop_id: Mapped[int] = mapped_column(
        ForeignKey("shops.id"), nullable=False, index=True
    )
    #: Denormalized owner, copied from the shop at creation. Every campaign
    #: query filters on this column, so no request can reach another account's
    #: campaign by guessing an id. Nullable only for pre-authentication rows,
    #: which are unreachable through the API.
    owner_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    # Raw transcript is immutable once captured (Source of Truth §7).
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # Detected language, duration, segments, confidence, provider, fallback flags.
    transcript_meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CampaignStatus.CAPTURED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    shop: Mapped[Shop] = relationship(back_populates="campaigns")
    jobs: Mapped[list["Job"]] = relationship(back_populates="campaign")
    audit_events: Mapped[list["AuditEvent"]] = relationship(back_populates="campaign")
    fact_sheets: Mapped[list["FactSheetRecord"]] = relationship(
        back_populates="campaign", order_by="FactSheetRecord.version"
    )


class FactSheetRecord(Base):
    """Versioned fact sheet (Source of Truth §16, §45).

    - `draft_json` holds the full editable FactSheet (facts + extraction
      metadata: confidence, inferred, ambiguities, missing).
    - `facts_json` holds the canonical authoritative payload, written only at
      lock time. The SHA-256 fact hash and HMAC seal cover exactly this payload.
    - A locked version is immutable. Editing it creates a new draft version and
      marks the old one `superseded`.
    """

    __tablename__ = "fact_sheets"
    __table_args__ = (UniqueConstraint("campaign_id", "version", name="uq_fact_sheets_campaign_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default=FactSheetStatus.DRAFT, index=True
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("fact_sheets.id"), nullable=True
    )
    draft_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    facts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    tokens_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    seal: Mapped[str | None] = mapped_column(String(160), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    campaign: Mapped[Campaign] = relationship(back_populates="fact_sheets")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=JobStatus.QUEUED
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    campaign: Mapped[Campaign | None] = relationship(back_populates="jobs")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    campaign: Mapped[Campaign] = relationship(back_populates="audit_events")


class CampaignPlanRecord(Base):
    """Versioned campaign plan (Phase 3, handoff §3).

    Stores the tokenized creative plan per locked FactSheet version. Literal
    fact values never appear here — substitution happens at render time.
    """

    __tablename__ = "campaign_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    fact_sheet_id: Mapped[int] = mapped_column(
        ForeignKey("fact_sheets.id"), nullable=False, index=True
    )
    fact_hash: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    campaign: Mapped[Campaign] = relationship()
    fact_sheet: Mapped[FactSheetRecord] = relationship()


class AssetRecord(Base):
    """Generated campaign asset with full provenance (handoff §9).

    Covers caption/poster/voice/video assets. Binary data lives in AssetStorage
    (or Cloud Storage in a Firebase deployment); ``storage_path`` is relative.
    """

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaign_plans.id"), nullable=True
    )
    factsheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("fact_sheets.id"), nullable=True
    )
    fact_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="en-IN")
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    template: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asset_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", index=True
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provenance_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    campaign: Mapped[Campaign] = relationship()


class VerificationResultRecord(Base):
    """Guardian check result for one asset (Source of Truth §26, §45)."""

    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id"), nullable=False, index=True
    )
    check_name: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    asset: Mapped[AssetRecord] = relationship()


class VoiceProfileRecord(Base):
    """Consenting, owned voice profile (Phase 5A/5B, handoff §3).

    Reference audio lives in AssetStorage; provider-issued ``voice_id`` is
    persisted so a profile is reused — never re-created per campaign.
    """

    __tablename__ = "voice_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )
    shop_id: Mapped[int | None] = mapped_column(
        ForeignKey("shops.id"), nullable=True, index=True
    )
    owner_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    consent_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consent_record_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference_audio_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    reference_transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="mock")
    provider_voice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    supported_languages_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class PublishRecord(Base):
    """Publishing attempt with approval binding (Phase 8)."""

    __tablename__ = "publish_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("campaigns.id"), nullable=False, index=True
    )
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id"), nullable=True
    )
    owner_uid: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    caption_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asset_hashes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action_schema_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("audit_events.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="ready_for_review")
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_result_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )
