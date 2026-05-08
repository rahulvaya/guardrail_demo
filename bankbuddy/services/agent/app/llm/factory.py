"""LLM client factory."""
from __future__ import annotations

from bankbuddy_shared.interfaces.llm import ILLMClient

from ..settings import Settings
from .auth import build_auth
from .litellm_client import LiteLLMClient


def build_llm(settings: Settings) -> ILLMClient:
    """Build an LLM client. Always LiteLLM-backed in Phase 1; the provider
    is selected purely by the model prefix and base URL. Authentication is
    delegated to an :class:`~app.llm.auth.LLMAuthProvider`.
    """
    auth = build_auth(provider_hint=settings.llm_provider, api_key=settings.llm_api_key)
    return LiteLLMClient(
        model=settings.llm_model,
        auth=auth,
        base_url=settings.llm_base_url,
        provider_hint=settings.llm_provider,
        api_version=settings.llm_api_version,
    )
