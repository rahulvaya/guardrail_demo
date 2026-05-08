"""Shared contract for the BankBuddy Guardrails Service v1 API.

Both server (services/guardrails) and clients (services/agent and any
future consumer) import these models so the wire format stays in lockstep.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Decision = Literal["allow", "sanitize", "block"]
Stage = Literal["input", "output", "tool_output"]


class GuardrailsCheckRequest(BaseModel):
    """POST /v1/check request body."""

    policy_id: str | None = None
    stage: Stage
    text: str
    context: dict[str, Any] = Field(default_factory=dict)


class GuardrailsGuardOutcome(BaseModel):
    name: str
    decision: Decision
    reasons: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GuardrailsCheckResponse(BaseModel):
    """POST /v1/check response body."""

    decision: Decision
    sanitized_text: str
    stage: Stage
    policy_id: str
    duration_ms: float
    block_reasons: list[str] = Field(default_factory=list)
    block_categories: list[str] = Field(default_factory=list)
    guards: list[GuardrailsGuardOutcome] = Field(default_factory=list)
    request_id: str | None = None


class GuardrailsPolicySummary(BaseModel):
    id: str
    description: str = ""
    input_guards: list[str] = Field(default_factory=list)
    output_guards: list[str] = Field(default_factory=list)
    tool_output_guards: list[str] = Field(default_factory=list)
