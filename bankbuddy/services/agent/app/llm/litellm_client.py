"""LiteLLM-backed `ILLMClient`."""
from __future__ import annotations

import logging
from typing import Any

from bankbuddy_shared.interfaces.llm import ILLMClient, LLMError

from .auth import LLMAuthProvider, NoAuth

_logger = logging.getLogger("agent.llm")


class LiteLLMClient(ILLMClient):
    """Thin wrapper over `litellm.acompletion`.

    LiteLLM normalizes provider differences. Set ``LLM_MODEL`` using the
    LiteLLM-style prefix:

    - Ollama:      ``ollama/llama3.1:8b``  (with LLM_BASE_URL=http://host:11434)
    - OpenAI:      ``openai/gpt-4o-mini``
    - Azure:       ``azure/<deployment>``  (plus LLM_BASE_URL / LLM_API_VERSION)
    - Bedrock:     ``bedrock/anthropic.claude-3-haiku-20240307-v1:0``
    - vLLM/local:  ``openai/<model>``      (with LLM_BASE_URL pointing at vLLM)

    Authentication is delegated to an :class:`LLMAuthProvider` so the transport
    code stays free of credential-source details.
    """

    def __init__(
        self,
        model: str,
        auth: LLMAuthProvider | None = None,
        base_url: str | None = None,
        provider_hint: str | None = None,
        api_version: str | None = None,
    ) -> None:
        if "/" not in model and provider_hint:
            model = f"{provider_hint}/{model}"
        self._default_model = model
        self._base_url = base_url
        self._api_version = api_version
        self._auth: LLMAuthProvider = auth or NoAuth()
        _logger.info(
            "LiteLLMClient: model=%s auth=%s base_url=%s api_version=%s",
            self._default_model,
            self._auth.name,
            self._base_url,
            self._api_version,
        )

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
        try:
            import litellm
        except ImportError as e:
            raise LLMError("litellm not installed") from e

        call_kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens
        self._auth.apply(call_kwargs)
        if self._base_url:
            call_kwargs["api_base"] = self._base_url
        if self._api_version:
            call_kwargs["api_version"] = self._api_version
        if tools:
            call_kwargs["tools"] = tools
        call_kwargs.update(kwargs)

        try:
            resp = await litellm.acompletion(**call_kwargs)
        except Exception as e:
            raise LLMError(f"LLM call failed: {e}") from e

        return resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)

    async def embed(self, text: str, *, model: str | None = None) -> list[float]:
        try:
            import litellm
        except ImportError as e:
            raise LLMError("litellm not installed") from e
        call_kwargs: dict[str, Any] = {
            "model": model or self._default_model,
            "input": [text],
        }
        self._auth.apply(call_kwargs)
        if self._base_url:
            call_kwargs["api_base"] = self._base_url
        if self._api_version:
            call_kwargs["api_version"] = self._api_version
        try:
            resp = await litellm.aembedding(**call_kwargs)
        except Exception as e:
            raise LLMError(f"embedding failed: {e}") from e
        return list(resp["data"][0]["embedding"])
