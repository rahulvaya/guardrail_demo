"""LLM client interface.

Default implementation wraps LiteLLM, which itself routes to OpenAI,
Azure OpenAI, Bedrock, Vertex, Ollama, vLLM, etc.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """Raised when the underlying LLM call fails."""


class ILLMClient(ABC):
    """Provider-agnostic LLM client."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Return a chat completion in OpenAI-compatible shape."""

    @abstractmethod
    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        """Return an embedding vector for `text`."""
