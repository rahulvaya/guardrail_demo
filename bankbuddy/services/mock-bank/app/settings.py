"""Configuration for mock-bank.

All settings come from environment variables (twelve-factor).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    # Service
    service_name: str = "mock-bank"
    port: int = 8200

    # Database (bank schema)
    postgres_host: str = Field(default="postgres", alias="POSTGRES_HOST")
    postgres_port: int = Field(default=5432, alias="POSTGRES_PORT")
    postgres_db: str = Field(default="bankbuddy", alias="POSTGRES_DB")
    bank_db_user: str = Field(default="bank_user", alias="BANK_DB_USER")
    bank_db_password: str = Field(default="bank_pw", alias="BANK_DB_PASSWORD")

    # Seeding
    seed_on_startup: bool = Field(default=True, alias="MOCK_BANK_SEED")

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.bank_db_user}:{self.bank_db_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
