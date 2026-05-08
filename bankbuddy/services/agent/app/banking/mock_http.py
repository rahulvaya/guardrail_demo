"""HTTP adapter that calls the mock-bank service over the internal network.

Implements `IBankingService` so the rest of the agent stays vendor-agnostic.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from bankbuddy_shared.interfaces.banking import BankingError, IBankingService


class MockBankHttpClient(IBankingService):
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _client(self, user_id: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={"X-User-Id": user_id},
        )

    async def _get(self, user_id: str, path: str, **params: Any) -> Any:
        async with self._client(user_id) as client:
            try:
                resp = await client.get(path, params={k: v for k, v in params.items() if v is not None})
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise BankingError(f"{e.response.status_code}: {e.response.text}") from e
            except httpx.HTTPError as e:
                raise BankingError(str(e)) from e

    async def _post(self, user_id: str, path: str, json: dict[str, Any]) -> Any:
        async with self._client(user_id) as client:
            try:
                resp = await client.post(path, json=json)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as e:
                raise BankingError(f"{e.response.status_code}: {e.response.text}") from e
            except httpx.HTTPError as e:
                raise BankingError(str(e)) from e

    async def get_accounts(self, user_id: str) -> list[dict[str, Any]]:
        return await self._get(user_id, "/accounts")

    async def get_transactions(
        self,
        user_id: str,
        account_id: str,
        *,
        limit: int = 10,
        since: date | None = None,
    ) -> list[dict[str, Any]]:
        params = {"limit": limit}
        if since is not None:
            params["since"] = since.isoformat()
        return await self._get(user_id, f"/accounts/{account_id}/transactions", **params)

    async def transfer(
        self,
        user_id: str,
        from_account_id: str,
        to_account_id: str,
        amount: Decimal,
        memo: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "from_account_id": from_account_id,
            "to_account_id": to_account_id,
            "amount": str(amount),
            "memo": memo,
        }
        return await self._post(user_id, "/transfers", body)

    async def block_card(self, user_id: str, card_id: str, reason: str) -> dict[str, Any]:
        return await self._post(user_id, f"/cards/{card_id}/block", {"reason": reason})

    async def find_atms(self, postal_code: str, radius_km: float = 5.0) -> list[dict[str, Any]]:
        # No user context needed; pass a placeholder header to satisfy the contract.
        return await self._get("anonymous", "/atms", postal_code=postal_code, radius_km=radius_km)

    async def check_loan_eligibility(
        self, user_id: str, amount: Decimal, term_months: int
    ) -> dict[str, Any]:
        return await self._post(
            user_id,
            "/loans/eligibility",
            {"amount": str(amount), "term_months": term_months},
        )
