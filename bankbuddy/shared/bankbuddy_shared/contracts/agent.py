"""Agent-level DTOs exchanged between API <-> Agent (internal only)."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .principal import Principal


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None


class AgentInvokeRequest(BaseModel):
    """Request from the `api` service to the `agent` service."""

    session_id: str
    message: str
    principal: Principal
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInvokeResponse(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
