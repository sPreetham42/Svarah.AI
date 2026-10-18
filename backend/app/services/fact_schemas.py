"""Pydantic schemas for the FactSheet.

This is the structured, schema-validated representation produced by extraction
and edited by humans before locking (Source of Truth §14, §15).

Schema note: extraction metadata (`extraction_confidence`, `inferred`,
`ambiguities`, `missing`) is *not* part of the canonical hashed payload — only
`business`, `offer`, `languages` are authoritative.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ambiguity(_Model):
    field: str = Field(min_length=1, max_length=120)
    note: str = Field(min_length=1, max_length=500)
    candidates: list[str] = Field(default_factory=list)

    @field_validator("field", "note")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class BusinessFacts(_Model):
    name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)


class OfferFacts(_Model):
    product: list[str] = Field(default_factory=list)
    discount_percent: float | None = Field(default=None, ge=0, le=100)
    discount_flat: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    quantity: float | None = Field(default=None, gt=0)
    audience: list[str] = Field(default_factory=list)
    days: list[str] = Field(default_factory=list)
    date_start: str | None = Field(default=None, max_length=64)
    date_end: str | None = Field(default=None, max_length=64)
    start_time: str | None = Field(default=None, max_length=16)
    end_time: str | None = Field(default=None, max_length=16)
    conditions: list[str] = Field(default_factory=list)
    location: str | None = Field(default=None, max_length=200)


class FactSheet(_Model):
    business: BusinessFacts = Field(default_factory=BusinessFacts)
    offer: OfferFacts = Field(default_factory=OfferFacts)
    languages: list[str] = Field(default_factory=list)
    extraction_confidence: dict[str, float] = Field(default_factory=dict)
    inferred: list[str] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)

    @field_validator("extraction_confidence")
    @classmethod
    def _confidence_range(cls, value: dict[str, float]) -> dict[str, float]:
        for key, score in value.items():
            if not key.strip():
                raise ValueError("Confidence keys must be non-empty field paths.")
            if not 0.0 <= float(score) <= 1.0:
                raise ValueError(f"Confidence for {key!r} must be within 0..1.")
        return value

    @field_validator("inferred", "missing")
    @classmethod
    def _clean_paths(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item and item.strip()]


# ── PATCH models (human edits) ────────────────────────────────────────
class BusinessPatch(_Model):
    name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)


class OfferPatch(_Model):
    product: list[str] | None = None
    discount_percent: float | str | None = None
    discount_flat: float | str | None = None
    price: float | str | None = None
    quantity: float | str | None = None
    audience: list[str] | None = None
    days: list[str] | None = None
    date_start: str | None = Field(default=None, max_length=64)
    date_end: str | None = Field(default=None, max_length=64)
    start_time: str | None = Field(default=None, max_length=16)
    end_time: str | None = Field(default=None, max_length=16)
    conditions: list[str] | None = None
    location: str | None = Field(default=None, max_length=200)


class FactSheetPatch(_Model):
    business: BusinessPatch | None = None
    offer: OfferPatch | None = None
    languages: list[str] | None = None
