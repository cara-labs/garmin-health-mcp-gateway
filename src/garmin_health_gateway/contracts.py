"""Evidence contracts for the activity-analysis tools (existing tools are unchanged)."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Availability = Literal[
    "available",
    "partial",
    "not_found",
    "not_recorded",
    "not_processed",
    "unsupported",
    "pending",
    "error",
]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceSource(ContractModel):
    """Identity and time evidence; retrieval time is not observation/effective time."""

    id: str = Field(min_length=1, max_length=256)
    provider: str = Field(min_length=1, max_length=256)
    kind: Literal["recorded", "modeled", "user_reported", "derived", "configured"]
    observed_at: datetime | None = None
    collected_at: datetime | None = None
    processed_at: datetime | None = None
    effective_at: datetime | None = None
    processing_version: str | None = None
    attribution: str | None = None

    @field_validator("observed_at", "collected_at", "processed_at", "effective_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("evidence timestamps require a timezone")
        return value


class FieldEvidence(ContractModel):
    """Metadata applies to one field or a documented repeated-field path."""

    unit: str | None = None
    unit_missing_reason: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    availability: Availability
    missing_reason: str | None = None
    observed_at: datetime | None = None
    limitations: list[str] = Field(default_factory=list)

    @field_validator("observed_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        return EvidenceSource.aware_timestamp(value)

    @model_validator(mode="after")
    def explain_absence(self) -> FieldEvidence:
        if self.unit is None and not self.unit_missing_reason:
            raise ValueError("unknown units require unit_missing_reason")
        if self.availability not in ("available", "partial") and not self.missing_reason:
            raise ValueError("unavailable fields require missing_reason")
        if self.availability == "available" and self.missing_reason is not None:
            raise ValueError("available fields cannot have missing_reason")
        return self


class AnalysisError(ContractModel):
    code: str
    message: str
    retryable: bool = False
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        return EvidenceSource.aware_timestamp(value)


class Freshness(ContractModel):
    """Last successful evidence and latest attempt are separate, including failed attempts."""

    last_success_at: datetime | None = None
    last_attempt_at: datetime | None = None
    stale: bool | None = None
    stale_reason: str | None = None

    @field_validator("last_success_at", "last_attempt_at")
    @classmethod
    def aware_timestamp(cls, value: datetime | None) -> datetime | None:
        return EvidenceSource.aware_timestamp(value)


class AnalysisResponse(ContractModel):
    schema_version: Literal["activity-analysis/1"] = "activity-analysis/1"
    availability: Availability
    activity_id: int | None = Field(default=None, gt=0)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    fields: dict[str, FieldEvidence] = Field(default_factory=dict)
    sources: list[EvidenceSource] = Field(default_factory=list)
    freshness: Freshness = Field(default_factory=Freshness)
    errors: list[AnalysisError] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    coverage: dict[str, JsonValue] = Field(default_factory=dict)
    next_cursor: str | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> AnalysisResponse:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        for path, metadata in self.fields.items():
            if set(metadata.source_ids) - set(ids):
                raise ValueError(f"unknown source id for {path}")
            # Concrete top-level fields get value/absence validation. Wildcard paths
            # describe repeated samples; their per-row missing reasons remain in data.
            if path in self.data:
                value = self.data[path]
                if value is None and metadata.availability == "available":
                    raise ValueError(f"null field {path} cannot be available")
                if value is not None and metadata.availability not in ("available", "partial"):
                    raise ValueError(f"present field {path} cannot be unavailable")

        def finite(value: JsonValue) -> None:
            if isinstance(value, float) and not math.isfinite(value):
                raise ValueError("non-finite measurements must be null with a missing reason")
            if isinstance(value, dict):
                for item in value.values():
                    finite(item)
            elif isinstance(value, list):
                for item in value:
                    finite(item)

        finite(self.data)
        finite(self.coverage)
        return self

    def as_dict(self) -> dict:
        """Keep explicit nulls; serialize aware evidence times as ISO-8601."""
        return self.model_dump(mode="json")


def field_evidence(
    value: JsonValue,
    *,
    unit: str | None,
    source_ids: list[str],
    missing_reason: str = "not_recorded",
    unit_missing_reason: str = "unknown",
) -> FieldEvidence:
    """Only None means absent: a recorded zero, False, or empty collection survives."""
    return FieldEvidence(
        unit=unit,
        unit_missing_reason=unit_missing_reason if unit is None else None,
        source_ids=source_ids,
        availability="not_recorded" if value is None else "available",
        missing_reason=missing_reason if value is None else None,
    )
