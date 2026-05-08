"""Chat-level DTOs exchanged between UI <-> API."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ChatRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        description="Conversation thread id. If null, the API will create one.",
    )
    message: str


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    messages: list[ChatMessage] = Field(default_factory=list)
    trace: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Diagnostic trace of the request flow: guardrail decisions, "
            "tool calls, blocked stage, etc. Surfaced to the UI for the "
            "live request-flow panel."
        ),
    )
