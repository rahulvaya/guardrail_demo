"""Banking domain service interface.

Implementations:
    - MockBankHttpClient       (default; calls the mock-bank service)
    - RealCoreBankingClient    (future; calls a real core-banking system)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Any


class BankingError(Exception):
    """Raised on any banking backend failure."""


class IBankingService(ABC):
    """Banking operations surfaced as agent tools."""

    @abstractmethod
    async def get_accounts(self, user_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def get_transactions(
        self,
        user_id: str,
        account_id: str,
        *,
        limit: int = 10,
        since: date | None = None,
    ) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def transfer(
        self,
        user_id: str,
        from_account_id: str,
        to_account_id: str,
        amount: Decimal,
        memo: str | None = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def block_card(self, user_id: str, card_id: str, reason: str) -> dict[str, Any]: ...

    @abstractmethod
    async def find_atms(self, postal_code: str, radius_km: float = 5.0) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def check_loan_eligibility(
        self, user_id: str, amount: Decimal, term_months: int
    ) -> dict[str, Any]: ...
