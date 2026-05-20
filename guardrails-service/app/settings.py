"""Guardrails service settings.

Note: the imported guard implementations (under `app.core`) reference a
`Settings` class via `from ..settings import Settings`. We provide a
shim here that exposes the few fields they need; the service itself
does NOT use this for pipeline construction (policies drive that).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service-level settings.

    Only fields actually read by the service or referenced by the copied
    guard modules are listed. Per-guard tuning happens in policy YAML.
    """

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: str = "guardrails"
    port: int = 8001

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    # Auth mode for /v1/* endpoints:
    #   static        - validate Authorization: Bearer <GUARDRAILS_INTERNAL_TOKEN>
    #   aad           - validate Microsoft Entra ID (Azure AD) JWTs only
    #   static_or_aad - accept either (default)
    auth_mode: str = Field(alias="GUARDRAILS_AUTH_MODE", default="static_or_aad")

    # Static bearer token. Required for `static` and `static_or_aad` modes.
    internal_token: str = Field(
        alias="GUARDRAILS_INTERNAL_TOKEN",
        default="please-rotate-this-token",
    )

    # ---- AAD / Microsoft Entra ID ----
    aad_tenant_id: str | None = Field(default=None, alias="GUARDRAILS_AAD_TENANT_ID")
    aad_audience: str | None = Field(default=None, alias="GUARDRAILS_AAD_AUDIENCE")
    aad_allowed_appids: str | None = Field(default=None, alias="GUARDRAILS_AAD_ALLOWED_APPIDS")
    aad_issuer: str | None = Field(default=None, alias="GUARDRAILS_AAD_ISSUER")
    aad_jwks_uri: str | None = Field(default=None, alias="GUARDRAILS_AAD_JWKS_URI")

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------
    # Default policy applied when the request omits policy_id.
    default_policy_id: str = Field(
        alias="GUARDRAILS_DEFAULT_POLICY_ID",
        default="default",
    )

    # Directory containing *.yaml policy bundles. Mounted into the image
    # at build time but overridable for hot-swapping in dev or by
    # supplying a ConfigMap volume mount in Kubernetes.
    policies_dir: str = Field(
        alias="GUARDRAILS_POLICIES_DIR",
        default="/app/app/policies",
    )

    # ------------------------------------------------------------------
    # Per-request overrides
    # ------------------------------------------------------------------
    # When true, /v1/check accepts an `overrides` dict that lets the
    # caller tune a whitelisted subset of guard fields on a single
    # request (e.g. raise `min_confidence` for a fraud-ops query).
    # Server-side policy still controls which guards run; consumers
    # cannot enable, disable, or re-credential guards via overrides.
    allow_request_overrides: bool = Field(
        default=True,
        alias="GUARDRAILS_ALLOW_REQUEST_OVERRIDES",
    )

    # CSV of guard-config keys consumers MAY override per request.
    # Anything not listed here is rejected with HTTP 400.
    overridable_keys: str = Field(
        default="min_confidence,severity_threshold,max_chars,block_threshold,warn_threshold,min_ratio,min_length,mode",
        alias="GUARDRAILS_OVERRIDABLE_KEYS",
    )

    # CSV of guard-config keys that are NEVER overridable, even if the
    # operator widens `overridable_keys` by mistake. Security boundary.
    forbidden_override_keys: str = Field(
        default="enabled,fail_open,endpoint,api_key,aad_token,api_version,language",
        alias="GUARDRAILS_FORBIDDEN_OVERRIDE_KEYS",
    )

    def overridable_keys_set(self) -> set[str]:
        return {k.strip() for k in self.overridable_keys.split(",") if k.strip()}

    def forbidden_override_keys_set(self) -> set[str]:
        return {k.strip() for k in self.forbidden_override_keys.split(",") if k.strip()}

    # ---- Fields below are read by copied guard modules ----
    # Master switch retained for compatibility; the service ignores it
    # and lets policies decide which guards run.
    guardrails_enabled: bool = Field(default=True, alias="GUARDRAILS_ENABLED")
    guardrails_block_message: str = Field(
        default="I'm sorry - I can't help with that request.",
        alias="GUARDRAILS_BLOCK_MESSAGE",
    )

    # Azure AI Content Safety (used by azure-content-safety, azure-groundedness,
    # azure-task-adherence guards). All three reuse the same endpoint + key.
    azure_content_safety_endpoint: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_ENDPOINT")
    azure_content_safety_key: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_KEY")
    # Optional pre-fetched AAD token (skips DefaultAzureCredential).
    # Must be scoped to https://cognitiveservices.azure.com/.default.
    azure_content_safety_aad_token: str | None = Field(
        default=None, alias="AZURE_CONTENT_SAFETY_AAD_TOKEN"
    )

    # Azure AI Language (used by azure-pii-detection guard)
    azure_language_endpoint: str | None = Field(default=None, alias="AZURE_LANGUAGE_ENDPOINT")
    azure_language_key: str | None = Field(default=None, alias="AZURE_LANGUAGE_KEY")
    azure_language_aad_token: str | None = Field(
        default=None, alias="AZURE_LANGUAGE_AAD_TOKEN"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
