"""Banking adapter factory.

Add new backends here. No other code in the agent should care which backend
is in use - everyone depends only on `IBankingService`.
"""
from __future__ import annotations

from bankbuddy_shared.interfaces.banking import IBankingService

from ..settings import Settings
from .mock_http import MockBankHttpClient


def build_banking(settings: Settings) -> IBankingService:
    backend = settings.banking_backend.lower()
    if backend == "mock":
        return MockBankHttpClient(base_url=settings.mock_bank_url)
    if backend == "real":
        raise NotImplementedError("real core-banking adapter not implemented; see docs/cloud-portability.md")
    raise ValueError(f"unknown BANKING_BACKEND: {settings.banking_backend}")
