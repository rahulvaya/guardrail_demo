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

    # Bearer token required on every /v1/* request. Callers send
    # Authorization: Bearer <token>.
    internal_token: str = Field(
        alias="GUARDRAILS_INTERNAL_TOKEN",
        default="please-rotate-this-token",
    )

    # Default policy applied when the request omits policy_id.
    default_policy_id: str = Field(
        alias="GUARDRAILS_DEFAULT_POLICY_ID",
        default="bankbuddy-default",
    )

    # Directory containing *.yaml policy bundles. Mounted into the image
    # at build time but overridable for hot-swapping in dev.
    policies_dir: str = Field(
        alias="GUARDRAILS_POLICIES_DIR",
        default="/app/services/guardrails/app/policies",
    )

    # ---- Fields below are read by copied guard modules ----
    # Master switch retained for compatibility; the service ignores it
    # and lets policies decide which guards run.
    guardrails_enabled: bool = Field(default=True, alias="GUARDRAILS_ENABLED")
    guardrails_block_message: str = Field(
        default="I'm sorry - I can't help with that request.",
        alias="GUARDRAILS_BLOCK_MESSAGE",
    )

    # Azure AI Content Safety (used by azure-content-safety guard)
    azure_content_safety_endpoint: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_ENDPOINT")
    azure_content_safety_key: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_KEY")

    # Azure AI Language (used by azure-pii-detection guard)
    azure_language_endpoint: str | None = Field(default=None, alias="AZURE_LANGUAGE_ENDPOINT")
    azure_language_key: str | None = Field(default=None, alias="AZURE_LANGUAGE_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
