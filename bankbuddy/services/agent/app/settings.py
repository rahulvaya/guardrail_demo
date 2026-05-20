"""Agent service settings."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: str = "agent"
    port: int = 8100

    # Internal authentication (api -> agent)
    internal_token: str = Field(alias="AGENT_INTERNAL_TOKEN", default="please-rotate-this-token")

    # Provider selection
    agent_provider: str = Field(default="langgraph", alias="AGENT_PROVIDER")

    # Optional override for the LLM system prompt. When unset, the provider
    # uses its built-in banking-focused prompt. Set this to broaden / replace
    # the assistant's persona without a code change.
    agent_system_prompt: str | None = Field(default=None, alias="AGENT_SYSTEM_PROMPT")

    # LLM gateway (LiteLLM)
    llm_provider: str = Field(default="ollama", alias="LLM_PROVIDER")
    llm_model: str = Field(default="llama3.1:8b", alias="LLM_MODEL")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    llm_api_version: str | None = Field(default=None, alias="LLM_API_VERSION")

    # Banking
    banking_backend: str = Field(default="mock", alias="BANKING_BACKEND")
    mock_bank_url: str = Field(default="http://mock-bank:8200", alias="MOCK_BANK_URL")

    # Postgres for LangGraph checkpointer (agent_memory schema)
    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="bankbuddy", alias="POSTGRES_DB")
    agent_db_user: str = Field(default="agent_user", alias="AGENT_DB_USER")
    agent_db_password: str = Field(default="agent_pw", alias="AGENT_DB_PASSWORD")

    checkpointer: str = Field(default="memory", alias="AGENT_CHECKPOINTER")

    # =========================================================================
    # GUARDRAILS - single place for every guardrails knob
    # -------------------------------------------------------------------------
    # ALL guardrails-related settings are listed here so a user can discover
    # and tune everything from one file. They fall into three groups:
    #
    #   (1) AGENT-SIDE CONNECTION                  - consumed by this service
    #   (2) GUARD CATALOG (per-guard ENABLED)      - consumed by guardrails-svc
    #   (3) PROVIDER CREDENTIALS (Azure CS / Lang) - consumed by guardrails-svc
    #
    # All three groups are wired from the SAME `.env` file by docker-compose
    # (the agent and the guardrails containers both `env_file: .env`), so
    # changing any value here -> change the env var -> `docker compose up -d`
    # picks it up.
    #
    # Runtime flow (env -> pipeline):
    #   1. docker compose injects env vars from `.env` into both containers
    #   2. pydantic-settings populates this Settings instance from the env
    #   3. main.py lifespan reads the AGENT-SIDE fields and constructs
    #      RemoteGuardrailPipeline(base_url=..., token=..., policy_id=...,
    #      timeout_seconds=..., block_message=...)
    #   4. The pipeline is stored on app.state.guardrails and passed to
    #      every LangGraph provider via build_agent(...)
    #   5. The guardrails-service reads the GUARD CATALOG and PROVIDER
    #      CREDENTIAL fields from its own copy of the same env vars and
    #      assembles its policy/guard runtime.
    # =========================================================================

    # ---- (1) AGENT-SIDE CONNECTION (consumed by this service) ----

    # Master switch. When false, the agent skips all guardrails calls.
    guardrails_enabled: bool = Field(default=False, alias="GUARDRAILS_ENABLED")

    # Base URL of the guardrails service (internal Docker DNS in dev).
    guardrails_url: str = Field(default="http://guardrails:8001", alias="GUARDRAILS_URL")

    # Bearer token presented to the guardrails service.
    guardrails_internal_token: str = Field(
        default="please-rotate-this-token",
        alias="GUARDRAILS_INTERNAL_TOKEN",
    )

    # Policy bundle the guardrails service should apply for this agent.
    guardrails_policy_id: str = Field(default="bankbuddy-default", alias="GUARDRAILS_POLICY_ID")

    # HTTP timeout per /v1/check call (seconds).
    # Input timeouts fail-closed (BLOCK); output timeouts fail-open (ALLOW).
    guardrails_timeout_seconds: float = Field(default=5.0, alias="GUARDRAILS_TIMEOUT_SECONDS")

    # Polite text returned to the user when any guard BLOCKS.
    guardrails_block_message: str = Field(
        default="I'm sorry - I can't help with that request. Please rephrase or ask about your accounts, cards, transfers, ATMs, or loans.",
        alias="GUARDRAILS_BLOCK_MESSAGE",
    )

    # ---- (2) GUARD CATALOG - per-guard enable flags ----
    # These mirror the GUARD_<NAME>_ENABLED env vars read by the guardrails
    # service registry. Listed here so the full guard catalog is discoverable
    # from one place. Tunable parameters per guard flow through
    # GUARD_<NAME>_CONFIG JSON env vars (see guardrails-service/.env.example
    # and each guard's docstring).
    #
    # Input-stage guards (run before the LLM):
    guard_azure_content_safety_enabled: bool = Field(default=True,  alias="GUARD_AZURE_CONTENT_SAFETY_ENABLED")
    guard_azure_pii_detection_enabled:  bool = Field(default=True,  alias="GUARD_AZURE_PII_DETECTION_ENABLED")
    guard_token_limit_enabled:          bool = Field(default=False, alias="GUARD_TOKEN_LIMIT_ENABLED")
    guard_banned_substrings_enabled:    bool = Field(default=False, alias="GUARD_BANNED_SUBSTRINGS_ENABLED")
    guard_prompt_injection_enabled:     bool = Field(default=False, alias="GUARD_PROMPT_INJECTION_ENABLED")
    guard_pii_detect_enabled:           bool = Field(default=False, alias="GUARD_PII_DETECT_ENABLED")
    guard_banking_relevance_enabled:    bool = Field(default=False, alias="GUARD_BANKING_RELEVANCE_ENABLED")
    # Output-stage guards (run on the LLM response):
    guard_output_pii_redact_enabled:    bool = Field(default=False, alias="GUARD_OUTPUT_PII_REDACT_ENABLED")
    guard_secret_leak_enabled:          bool = Field(default=False, alias="GUARD_SECRET_LEAK_ENABLED")
    guard_toxicity_enabled:             bool = Field(default=False, alias="GUARD_TOXICITY_ENABLED")
    guard_competitor_mentions_enabled:  bool = Field(default=False, alias="GUARD_COMPETITOR_MENTIONS_ENABLED")

    # ---- (3) PROVIDER CREDENTIALS - Azure AI resources used by guards ----
    # Consumed by the guardrails service; listed here for visibility so a user
    # can set every guardrails-related variable from one location.

    # Azure AI Content Safety (used by azure-content-safety guard)
    azure_content_safety_endpoint: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_ENDPOINT")
    azure_content_safety_key:      str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_KEY")
    # Optional pre-fetched bearer token (used when key is empty and you want
    # to skip the managed-identity round-trip).
    azure_content_safety_aad_token: str | None = Field(default=None, alias="AZURE_CONTENT_SAFETY_AAD_TOKEN")

    # Azure AI Language (used by azure-pii-detection guard). Defaults to the
    # Content Safety endpoint when both APIs are exposed by the same
    # multi-service Cognitive Services / AI Services resource.
    azure_language_endpoint:  str | None = Field(default=None, alias="AZURE_LANGUAGE_ENDPOINT")
    azure_language_key:       str | None = Field(default=None, alias="AZURE_LANGUAGE_KEY")
    azure_language_aad_token: str | None = Field(default=None, alias="AZURE_LANGUAGE_AAD_TOKEN")

    def guard_flags(self) -> dict[str, bool]:
        """Return {guard-name: enabled} for every known guard.

        Useful for diagnostics and as a one-call summary of the guard catalog.
        Names use the canonical hyphenated form (matching each guard's `name`
        attribute on the guardrails service side).
        """
        return {
            "azure-content-safety": self.guard_azure_content_safety_enabled,
            "azure-pii-detection":  self.guard_azure_pii_detection_enabled,
            "token-limit":          self.guard_token_limit_enabled,
            "banned-substrings":    self.guard_banned_substrings_enabled,
            "prompt-injection":     self.guard_prompt_injection_enabled,
            "pii-detect":           self.guard_pii_detect_enabled,
            "banking-relevance":    self.guard_banking_relevance_enabled,
            "output-pii-redact":    self.guard_output_pii_redact_enabled,
            "secret-leak":          self.guard_secret_leak_enabled,
            "toxicity":             self.guard_toxicity_enabled,
            "competitor-mentions":  self.guard_competitor_mentions_enabled,
        }

    @property
    def database_url(self) -> str:
        return (
            f"postgresql://{self.agent_db_user}:{self.agent_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
