"""v1 HTTP contract models for the guardrails service.

The canonical wire contract. SDK packages mirror these shapes; do not
break field names without bumping the `/v1/` prefix.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# All six checkpoints from the agent reference architecture, plus the
# friendly llm_input/llm_output aliases the agent emits.
StageLiteral = Literal[
    "api_input",
    "input",
    "llm_input",
    "tool_input",
    "output",
    "llm_output",
    "tool_output",
    "api_output",
]


class CheckRequest(BaseModel):
    """POST /v1/check request body."""

    policy_id: str | None = Field(
        default=None,
        description="Policy bundle to apply. Falls back to GUARDRAILS_DEFAULT_POLICY_ID.",
    )
    stage: StageLiteral
    text: str
    context: dict[str, Any] = Field(default_factory=dict)
    overrides: dict[str, dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Per-request guard-config overrides: {guard-name: {key: value}}. "
            "Only keys in GUARDRAILS_OVERRIDABLE_KEYS are accepted; anything "
            "in GUARDRAILS_FORBIDDEN_OVERRIDE_KEYS (or unknown guards) is "
            "rejected with HTTP 400. Server policy controls which guards "
            "run; overrides only tune their thresholds."
        ),
    )


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
    stage: StageLiteral
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
    api_input_guards: list[str] = Field(default_factory=list)
    input_guards: list[str] = Field(default_factory=list)
    tool_input_guards: list[str] = Field(default_factory=list)
    output_guards: list[str] = Field(default_factory=list)
    tool_output_guards: list[str] = Field(default_factory=list)
    api_output_guards: list[str] = Field(default_factory=list)
