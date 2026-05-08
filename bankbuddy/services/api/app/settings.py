"""API service settings."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    service_name: str = "api"
    port: int = 8000

    # Auth
    auth_provider: str = Field(default="local-dev", alias="AUTH_PROVIDER")

    # Entra
    entra_tenant_id: str | None = Field(default=None, alias="ENTRA_TENANT_ID")
    entra_client_id: str | None = Field(default=None, alias="ENTRA_CLIENT_ID")
    entra_client_secret: str | None = Field(default=None, alias="ENTRA_CLIENT_SECRET")
    entra_redirect_uri: str | None = Field(default=None, alias="ENTRA_REDIRECT_URI")

    # App-issued JWT for the browser session
    app_jwt_secret: str = Field(default="change-me-please", alias="APP_JWT_SECRET")
    app_jwt_ttl_seconds: int = Field(default=3600, alias="APP_JWT_TTL_SECONDS")
    app_jwt_algorithm: str = "HS256"

    # CORS
    ui_origin: str = Field(default="http://localhost:8080", alias="PUBLIC_UI_ORIGIN")

    # Agent (internal)
    agent_internal_url: str = Field(default="http://agent:8100", alias="AGENT_INTERNAL_URL")
    agent_internal_token: str = Field(default="please-rotate-this-token", alias="AGENT_INTERNAL_TOKEN")


@lru_cache
def get_settings() -> Settings:
    return Settings()
