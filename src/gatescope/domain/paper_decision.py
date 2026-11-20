"""Bounded client decision claims, separate from server-verified market evidence."""
from __future__ import annotations

from datetime import timezone
from decimal import Decimal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class PatternDecisionReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    candidate_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    sequence: int = Field(gt=0, le=1_000_000, strict=True)


class CoinglassDecisionClaim(BaseModel):
    """A model/user claim, never relabeled as a verified provider response."""
    model_config = ConfigDict(extra="forbid")
    metric: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:-]+$")
    value: Decimal = Field(allow_inf_nan=False, ge=Decimal("-1e30"), le=Decimal("1e30"))
    unit: str = Field(min_length=1, max_length=40)
    source_at: AwareDatetime
    market_scope: str = Field(min_length=1, max_length=100)

    @field_validator("source_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)


class PaperDecisionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observed_at: AwareDatetime
    rationale: str = Field(min_length=1, max_length=2000)
    analysis_model: str | None = Field(default=None, min_length=1, max_length=80)
    pattern_events: list[PatternDecisionReference] = Field(default_factory=list, max_length=5)
    coinglass_claims: list[CoinglassDecisionClaim] = Field(default_factory=list, max_length=12)

    @field_validator("observed_at")
    @classmethod
    def utc(cls, value):
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def clocks_and_unique_references(self):
        keys = [(item.context_id, item.candidate_id, item.sequence) for item in self.pattern_events]
        if len(keys) != len(set(keys)):
            raise ValueError("DUPLICATE_DECISION_PATTERN_REFERENCE")
        if any(item.source_at > self.observed_at for item in self.coinglass_claims):
            raise ValueError("DECISION_CLAIM_FROM_FUTURE")
        if not self.rationale.strip():
            raise ValueError("DECISION_RATIONALE_REQUIRED")
        return self


def normalized_decision_context(value) -> dict:
    return PaperDecisionContext.model_validate(value).model_dump(mode="json")
