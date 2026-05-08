"""v1 HTTP contract models for the guardrails service.

Mirror these in `bankbuddy_shared/contracts/guardrails.py` so client and
server stay in lockstep.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CheckRequest(BaseModel):
    """POST /v1/check request body."""

    policy_id: str | None = Field(
        default=None,
        description="Policy bundle to apply. Falls back to GUARDRAILS_DEFAULT_POLICY_ID.",
    )
    stage: Literal["input", "output", "tool_output"]
    text: str
    context: dict[str, Any] = Field(default_factory=dict)


class GuardOutcome(BaseModel):
    name: str
    decision: Literal["allow", "sanitize", "block"]
    reasons: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CheckResponse(BaseModel):
    """POST /v1/check response body."""

    decision: Literal["allow", "sanitize", "block"]
    sanitized_text: str
    stage: Literal["input", "output", "tool_output"]
    policy_id: str
    duration_ms: float
    block_reasons: list[str] = Field(default_factory=list)
    block_categories: list[str] = Field(default_factory=list)
    guards: list[GuardOutcome] = Field(default_factory=list)
    request_id: str | None = None


class PolicySummary(BaseModel):
    """Item in GET /v1/policies."""

    id: str
    description: str = ""
    input_guards: list[str] = Field(default_factory=list)
    output_guards: list[str] = Field(default_factory=list)
    tool_output_guards: list[str] = Field(default_factory=list)
