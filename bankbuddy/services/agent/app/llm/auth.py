"""LLM authentication providers.

Authentication is decoupled from the LLM client behind a small abstraction so
new credential sources (API keys, Entra ID service principal, managed identity,
workload identity, AWS SigV4, etc.) can be added without touching transport
code.

Selection rules (in :func:`build_auth`):

1. ``LLM_API_KEY`` set                                -> :class:`ApiKeyAuth`
2. ``AZURE_OPENAI_AAD_TOKEN`` set                     -> :class:`StaticBearerAuth`
   (pre-fetched on host via ``az account get-access-token``; bypasses
   Conditional Access blocks on service principals by reusing the user's
   already-authenticated session)
3. provider hint is ``azure`` and SP env vars set     -> :class:`AzureAdAuth`
   (``AZURE_TENANT_ID`` + ``AZURE_CLIENT_ID`` + ``AZURE_CLIENT_SECRET``)
4. provider hint is ``azure`` and no key              -> :class:`AzureAdAuth`
   using :class:`~azure.identity.DefaultAzureCredential`
5. otherwise                                          -> :class:`NoAuth`
"""
from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Any

from bankbuddy_shared.interfaces.llm import LLMError

_logger = logging.getLogger("agent.llm.auth")

_AZURE_OPENAI_SCOPE = "https://cognitiveservices.azure.com/.default"


class LLMAuthProvider(ABC):
    """Strategy for injecting credentials into a LiteLLM call."""

    name: str = "abstract"

    @abstractmethod
    def apply(self, call_kwargs: dict[str, Any]) -> None:
        """Mutate ``call_kwargs`` to add whatever credential fields are needed."""


class NoAuth(LLMAuthProvider):
    """No credential injection. Used for local Ollama, vLLM, etc."""

    name = "none"

    def apply(self, call_kwargs: dict[str, Any]) -> None:  # noqa: D401
        return


class ApiKeyAuth(LLMAuthProvider):
    """Static API key (OpenAI, Azure with key auth enabled, Bedrock-via-key)."""

    name = "api-key"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise LLMError("ApiKeyAuth requires a non-empty key")
        self._api_key = api_key

    def apply(self, call_kwargs: dict[str, Any]) -> None:
        call_kwargs["api_key"] = self._api_key


class StaticBearerAuth(LLMAuthProvider):
    """Pre-fetched Entra ID bearer token (no live credential exchange).

    Useful when the runtime environment cannot satisfy Conditional Access
    on its own (e.g. Linux container) but the developer host can. The host
    runs ``az account get-access-token --resource
    https://cognitiveservices.azure.com`` and exports the result as
    ``AZURE_OPENAI_AAD_TOKEN``; this strategy injects it verbatim.

    Tokens typically last ~60-75 minutes; refresh with the host helper
    ``tools/refresh-aad-token.ps1`` when calls start returning 401.
    """

    name = "static-bearer"

    def __init__(self, token: str) -> None:
        if not token:
            raise LLMError("StaticBearerAuth requires a non-empty token")
        self._token = token

    def apply(self, call_kwargs: dict[str, Any]) -> None:
        call_kwargs["azure_ad_token"] = self._token


class AzureAdAuth(LLMAuthProvider):
    """Entra ID bearer token for Azure OpenAI.

    Uses ``ClientSecretCredential`` when tenant/client/secret env vars are
    provided, otherwise falls back to ``DefaultAzureCredential`` (managed
    identity, env vars, Azure CLI, etc.).

    Tokens are cached and refreshed 5 minutes before expiry.
    """

    name = "azure-ad"

    def __init__(
        self,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._expires_on: int = 0
        self._credential: Any = None
        self._mode: str = "default-credential"
        if tenant_id and client_id and client_secret:
            self._mode = "service-principal"

    def _ensure_credential(self) -> Any:
        if self._credential is not None:
            return self._credential
        try:
            from azure.identity import (
                ClientSecretCredential,
                DefaultAzureCredential,
            )
        except ImportError as e:  # pragma: no cover
            raise LLMError("azure-identity not installed") from e

        if self._mode == "service-principal":
            self._credential = ClientSecretCredential(
                tenant_id=self._tenant_id,  # type: ignore[arg-type]
                client_id=self._client_id,  # type: ignore[arg-type]
                client_secret=self._client_secret,  # type: ignore[arg-type]
            )
            _logger.info("AzureAdAuth: using ClientSecretCredential")
        else:
            self._credential = DefaultAzureCredential()
            _logger.info("AzureAdAuth: using DefaultAzureCredential")
        return self._credential

    def _get_token(self) -> str:
        now = int(time.time())
        if self._token and now < self._expires_on - 300:
            return self._token
        cred = self._ensure_credential()
        try:
            access = cred.get_token(_AZURE_OPENAI_SCOPE)
        except Exception as e:
            raise LLMError(f"Azure AD token fetch failed ({self._mode}): {e}") from e
        self._token = access.token
        self._expires_on = int(access.expires_on)
        _logger.info(
            "AzureAdAuth: token acquired (mode=%s expires_on=%s)",
            self._mode,
            self._expires_on,
        )
        return self._token

    def apply(self, call_kwargs: dict[str, Any]) -> None:
        call_kwargs["azure_ad_token"] = self._get_token()


def build_auth(provider_hint: str | None, api_key: str | None) -> LLMAuthProvider:
    """Pick the right auth strategy based on provider hint and configured key."""
    if api_key:
        return ApiKeyAuth(api_key)
    if (provider_hint or "").lower() == "azure":
        prefetched = os.environ.get("AZURE_OPENAI_AAD_TOKEN")
        if prefetched:
            _logger.info(
                "build_auth: using StaticBearerAuth from AZURE_OPENAI_AAD_TOKEN"
            )
            return StaticBearerAuth(prefetched)
        return AzureAdAuth(
            tenant_id=os.environ.get("AZURE_TENANT_ID"),
            client_id=os.environ.get("AZURE_CLIENT_ID"),
            client_secret=os.environ.get("AZURE_CLIENT_SECRET"),
        )
    return NoAuth()
