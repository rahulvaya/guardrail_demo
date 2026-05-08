"""Agent provider interface.

Implementations:
    - LangGraphAgentProvider     (default, Phase 1)
    - FoundryAgentProvider       (stub, future)
    - OpenAIAssistantProvider    (stub, future)
    - BedrockAgentProvider       (stub, future)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from ..contracts.agent import AgentInvokeRequest, AgentInvokeResponse


class AgentError(Exception):
    """Raised on agent execution failure."""


class IAgentProvider(ABC):
    """A conversational agent capable of executing banking tools."""

    @abstractmethod
    async def invoke(self, request: AgentInvokeRequest) -> AgentInvokeResponse:
        """Run one turn of the conversation and return the final reply."""

    @abstractmethod
    async def stream(self, request: AgentInvokeRequest) -> AsyncIterator[str]:
        """Stream the reply token-by-token. Optional; default impl may yield
        the full reply as a single chunk."""
