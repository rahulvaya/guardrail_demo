"""Settings for the Azure AI Evaluation integration.

Two credential tiers:
  1. Azure AI Project  — safety evaluators (Violence, Sexual, Self-harm,
                         Hate/Unfairness, Indirect-Attack, Protected-Material).
                         Set EVAL_AI_PROJECT_* env vars.
  2. Azure OpenAI      — quality evaluators (Groundedness, Coherence, Fluency,
                         Relevance, Similarity, Retrieval, QA).
                         Set EVAL_AZURE_OPENAI_* env vars.

Authentication priority (checked in order):
  a. EVAL_AZURE_OPENAI_API_KEY / AZURE_CONTENT_SAFETY_KEY (static key)
  b. EVAL_AZURE_AAD_TOKEN / AZURE_CONTENT_SAFETY_AAD_TOKEN (pre-fetched bearer)
  c. DefaultAzureCredential — auto-fetches a token from the Entra ID service
     principal (AZURE_CLIENT_ID + AZURE_CLIENT_SECRET + AZURE_TENANT_ID) already
     present in the container environment.  Token is cached for 55 minutes.

Endpoint fallback:
  When EVAL_AZURE_OPENAI_ENDPOINT is absent the module reuses
  AZURE_CONTENT_SAFETY_ENDPOINT (same multi-service Cognitive Services
  resource), so quality evaluators work once a deployment name is provided.
"""
from __future__ import annotations

import logging
import threading
import time
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# StaticTokenCredential — wraps a pre-fetched bearer token so it satisfies
# the azure-core TokenCredential protocol expected by azure-ai-evaluation SDK.
# ---------------------------------------------------------------------------

class _StaticTokenCredential:
    """Minimal TokenCredential that returns a single pre-fetched bearer token."""

    def __init__(self, token: str, expires_in: int = 3600) -> None:
        self._token = token
        self._expires_on = int(time.time()) + expires_in

    def get_token(self, *scopes, **kwargs):  # type: ignore[override]
        from azure.core.credentials import AccessToken  # type: ignore
        return AccessToken(self._token, self._expires_on)


# ---------------------------------------------------------------------------
# DefaultAzureCredential token cache
# Tokens are valid for ~60-75 min; we refresh 5 min before expiry.
# ---------------------------------------------------------------------------
_token_lock = threading.Lock()
_cached_token: str | None = None
_token_expires_at: float = 0.0  # monotonic seconds


def _fetch_sp_token(endpoint: str) -> str | None:
    """Fetch a bearer token for *endpoint* using the service-principal env vars.

    Uses ``azure-identity`` (already a direct dependency of the guardrails
    service).  Returns ``None`` and logs a warning on any failure so callers
    can fall back gracefully.
    """
    global _cached_token, _token_expires_at  # noqa: PLW0603
    now = time.monotonic()
    with _token_lock:
        if _cached_token and now < _token_expires_at:
            return _cached_token
        try:
            from azure.identity import ClientSecretCredential, DefaultAzureCredential  # type: ignore

            # Audience for all Cognitive Services / AI Services resources.
            scope = "https://cognitiveservices.azure.com/.default"
            try:
                cred = DefaultAzureCredential()
                tok = cred.get_token(scope)
            except Exception:
                # Narrow fallback: explicit ClientSecretCredential using
                # AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET.
                import os
                cred = ClientSecretCredential(
                    tenant_id=os.environ["AZURE_TENANT_ID"],
                    client_id=os.environ["AZURE_CLIENT_ID"],
                    client_secret=os.environ["AZURE_CLIENT_SECRET"],
                )
                tok = cred.get_token(scope)

            _cached_token = tok.token
            # expires_on is a Unix timestamp; cache with 5-minute safety margin.
            _token_expires_at = now + max(tok.expires_on - time.time() - 300, 60)
            _log.info("eval_settings: fetched AAD token via DefaultAzureCredential (expires in ~%.0fs)", _token_expires_at - now)
            return _cached_token
        except Exception as exc:  # noqa: BLE001
            _log.warning("eval_settings: failed to fetch AAD token — %s", exc)
            return None


class EvaluationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # ------------------------------------------------------------------
    # Azure AI Project  (safety evaluators via azure-ai-evaluation SDK)
    # ------------------------------------------------------------------
    eval_ai_project_subscription_id: str | None = Field(
        default=None, alias="EVAL_AI_PROJECT_SUBSCRIPTION_ID"
    )
    eval_ai_project_resource_group: str | None = Field(
        default=None, alias="EVAL_AI_PROJECT_RESOURCE_GROUP"
    )
    eval_ai_project_name: str | None = Field(
        default=None, alias="EVAL_AI_PROJECT_NAME"
    )
    # Optional: full project endpoint URL (takes priority over sub/rg/name
    # when present, e.g. "https://<name>.api.azureml.ms").
    eval_ai_project_endpoint: str | None = Field(
        default=None, alias="EVAL_AI_PROJECT_ENDPOINT"
    )

    # ------------------------------------------------------------------
    # Azure OpenAI  (quality / LLM-graded evaluators)
    # ------------------------------------------------------------------
    eval_azure_openai_endpoint: str | None = Field(
        default=None, alias="EVAL_AZURE_OPENAI_ENDPOINT"
    )
    eval_azure_openai_deployment: str = Field(
        default="gpt-4o", alias="EVAL_AZURE_OPENAI_DEPLOYMENT"
    )
    eval_azure_openai_api_key: str | None = Field(
        default=None, alias="EVAL_AZURE_OPENAI_API_KEY"
    )
    eval_azure_openai_api_version: str = Field(
        default="2024-08-01-preview", alias="EVAL_AZURE_OPENAI_API_VERSION"
    )
    # Pre-fetched Entra ID bearer token for the evaluation OpenAI resource.
    eval_azure_aad_token: str | None = Field(
        default=None, alias="EVAL_AZURE_AAD_TOKEN"
    )

    # ------------------------------------------------------------------
    # Fallback: reuse existing Content Safety / Language credentials
    # ------------------------------------------------------------------
    azure_content_safety_endpoint: str | None = Field(
        default=None, alias="AZURE_CONTENT_SAFETY_ENDPOINT"
    )
    azure_content_safety_key: str | None = Field(
        default=None, alias="AZURE_CONTENT_SAFETY_KEY"
    )
    azure_content_safety_aad_token: str | None = Field(
        default=None, alias="AZURE_CONTENT_SAFETY_AAD_TOKEN"
    )
    # Dedicated Content Safety resource for direct text:analyze fallback.
    # Set EVAL_CONTENT_SAFETY_ENDPOINT to a cognitiveservices.azure.com endpoint.
    # Used by safety evaluators when the Foundry RAI service is unavailable.
    eval_content_safety_endpoint: str | None = Field(
        default=None, alias="EVAL_CONTENT_SAFETY_ENDPOINT"
    )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def has_azure_ai_project(self) -> bool:
        """True when a valid Azure AI Foundry project is configured for safety evaluators.

        The azure-ai-evaluation safety evaluators (Violence, Sexual, etc.) require an
        actual Azure AI Foundry project — not a plain Cognitive Services endpoint.

        Accepted config:
          - Full triplet: EVAL_AI_PROJECT_SUBSCRIPTION_ID + RESOURCE_GROUP + NAME
          - Foundry oneDP endpoint: EVAL_AI_PROJECT_ENDPOINT that looks like an AI
            Foundry URL (*.services.ai.azure.com or *.api.azureml.ms or contains
            /api/projects/).  Plain cognitiveservices.azure.com endpoints do NOT work.
        """
        if self.eval_ai_project_endpoint:
            ep = self.eval_ai_project_endpoint.lower()
            # Accept only known Foundry endpoint patterns; reject plain CS endpoints.
            is_foundry = (
                "services.ai.azure.com" in ep
                or "api.azureml.ms" in ep
                or "/api/projects/" in ep
            )
            return is_foundry
        return bool(
            self.eval_ai_project_subscription_id
            and self.eval_ai_project_resource_group
            and self.eval_ai_project_name
        )

    @property
    def has_openai_config(self) -> bool:
        """True when enough config is present to run quality evaluators."""
        endpoint = self.openai_endpoint
        key = self.openai_api_key
        aad = self.eval_azure_aad_token or self.azure_content_safety_aad_token
        if endpoint and (key or aad) and self.eval_azure_openai_deployment:
            return True
        # Last resort: try to auto-fetch a token via DefaultAzureCredential.
        if endpoint and self.eval_azure_openai_deployment:
            return bool(_fetch_sp_token(endpoint))
        return False

    @property
    def has_content_safety_direct(self) -> bool:
        """True when a dedicated Content Safety endpoint is configured for direct text:analyze calls.

        Used as a fallback for safety evaluators when the Foundry RAI service is
        unavailable in the region.  Requires EVAL_CONTENT_SAFETY_ENDPOINT to be set.
        Auth uses AZURE_CONTENT_SAFETY_AAD_TOKEN (already fetched for the CS resource).
        """
        ep = self.eval_content_safety_endpoint
        token = self.azure_content_safety_aad_token or self.azure_content_safety_key
        return bool(ep and token)

    @property
    def content_safety_analyze_url(self) -> str | None:
        """Full URL for POST /contentsafety/text:analyze (api-version=2024-09-01)."""
        ep = self.eval_content_safety_endpoint
        if not ep:
            return None
        return f"{ep.rstrip('/')}/contentsafety/text:analyze?api-version=2024-09-01"

    @property
    def content_safety_auth_header(self) -> str | None:
        """Bearer token or API-key header value for the Content Safety resource."""
        if self.azure_content_safety_aad_token:
            return f"Bearer {self.azure_content_safety_aad_token}"
        if self.azure_content_safety_key:
            return None  # key auth uses Ocp-Apim-Subscription-Key header instead
        return None

    @property
    def content_safety_key_header(self) -> str | None:
        """API-key value when using subscription-key auth."""
        return self.azure_content_safety_key or None

    @property
    def openai_endpoint(self) -> str | None:
        return self.eval_azure_openai_endpoint or self.azure_content_safety_endpoint

    @property
    def openai_api_key(self) -> str | None:
        return self.eval_azure_openai_api_key or self.azure_content_safety_key

    @property
    def openai_aad_token(self) -> str | None:
        return self.eval_azure_aad_token or self.azure_content_safety_aad_token

    def build_azure_ai_project(self) -> str | dict | None:
        """Return the azure_ai_project value accepted by safety evaluator constructors.

        SDK 1.17+ accepts either:
          - a string ("OneDP" endpoint, checked via is_onedp_project())
          - a dict with subscription_id / resource_group_name / project_name
        Returns None when no project config is present.
        """
        if self.eval_ai_project_endpoint:
            # Pass as string so the SDK's is_onedp_project() check passes.
            return self.eval_ai_project_endpoint
        if not self.has_azure_ai_project:
            return None
        return {
            "subscription_id": self.eval_ai_project_subscription_id,
            "resource_group_name": self.eval_ai_project_resource_group,
            "project_name": self.eval_ai_project_name,
        }

    def get_credential(self):
        """Return a TokenCredential for Azure AI service calls.

        Priority:
          1. Pre-fetched AAD token (EVAL_AZURE_AAD_TOKEN or AZURE_CONTENT_SAFETY_AAD_TOKEN)
             wrapped in _StaticTokenCredential — works even when SP secret is expired.
          2. DefaultAzureCredential — used when service principal is healthy.
        """
        token = self.eval_azure_aad_token or self.azure_content_safety_aad_token
        if token:
            return _StaticTokenCredential(token)
        try:
            from azure.identity import DefaultAzureCredential  # type: ignore
            return DefaultAzureCredential()
        except ImportError:
            return None

    def build_model_config_dict(self) -> dict | None:
        """Return the model_config dict accepted by quality evaluator constructors.

        IMPORTANT: Do NOT include ``api_key`` or ``credential`` in this dict when
        using AAD/bearer-token auth.  The SDK validates the dict strictly and rejects
        credential objects inside it.  Pass the credential as a *separate* constructor
        argument (``credential=settings.get_credential()``) alongside this dict.
        """
        endpoint = self.openai_endpoint
        deployment = self.eval_azure_openai_deployment
        if not endpoint or not deployment:
            return None

        base: dict = {
            "type": "azure_openai",
            "azure_endpoint": endpoint,
            "azure_deployment": deployment,
            "api_version": self.eval_azure_openai_api_version,
        }

        # Include api_key only when a static key is explicitly configured.
        # For bearer-token (AAD) auth, omit api_key and let the evaluator
        # constructors receive ``credential=`` as a separate kwarg instead.
        api_key = self.openai_api_key
        if api_key:
            base["api_key"] = api_key

        return base


@lru_cache
def get_eval_settings() -> EvaluationSettings:
    return EvaluationSettings()
