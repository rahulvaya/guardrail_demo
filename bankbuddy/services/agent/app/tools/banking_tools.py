"""Banking tools exposed to the agent.

These translate LLM tool-calls into IBankingService method calls. The tool
*schema* (JSON Schema for the OpenAI / LiteLLM tools API) is generated here
so we don't need a vendor-specific decorator.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from bankbuddy_shared.interfaces.banking import BankingError, IBankingService


# OpenAI / LiteLLM compatible tool schemas
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_accounts",
            "description": "List the current customer's bank accounts with balances.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transactions",
            "description": "List recent transactions for one of the customer's accounts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                },
                "required": ["account_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer",
            "description": "Move money between two accounts owned by the current customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_account_id": {"type": "string"},
                    "to_account_id": {"type": "string"},
                    "amount": {"type": "number", "exclusiveMinimum": 0},
                    "memo": {"type": "string"},
                },
                "required": ["from_account_id", "to_account_id", "amount"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "block_card",
            "description": "Block one of the customer's payment cards (for theft, loss, fraud, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "card_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["card_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_atms",
            "description": "Find ATMs near a postal code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "postal_code": {"type": "string"},
                    "radius_km": {"type": "number", "default": 5.0},
                },
                "required": ["postal_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_loan_eligibility",
            "description": "Check whether the current customer is eligible for a loan of a given amount and term.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "exclusiveMinimum": 0},
                    "term_months": {"type": "integer", "minimum": 3, "maximum": 360},
                },
                "required": ["amount", "term_months"],
            },
        },
    },
]


class BankingToolDispatcher:
    """Dispatches a tool name + JSON args to the matching IBankingService method."""

    def __init__(self, banking: IBankingService) -> None:
        self._b = banking

    async def call(self, name: str, arguments: str | dict[str, Any], user_id: str) -> str:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        try:
            result = await self._dispatch(name, args, user_id)
        except BankingError as e:
            return json.dumps({"error": str(e)})
        return json.dumps(result, default=_json_default)

    async def _dispatch(self, name: str, args: dict[str, Any], user_id: str) -> Any:
        if name == "get_accounts":
            return await self._b.get_accounts(user_id)
        if name == "get_transactions":
            return await self._b.get_transactions(
                user_id,
                account_id=args["account_id"],
                limit=int(args.get("limit", 10)),
            )
        if name == "transfer":
            return await self._b.transfer(
                user_id,
                from_account_id=args["from_account_id"],
                to_account_id=args["to_account_id"],
                amount=Decimal(str(args["amount"])),
                memo=args.get("memo"),
            )
        if name == "block_card":
            return await self._b.block_card(user_id, card_id=args["card_id"], reason=args["reason"])
        if name == "find_atms":
            return await self._b.find_atms(
                postal_code=args["postal_code"],
                radius_km=float(args.get("radius_km", 5.0)),
            )
        if name == "check_loan_eligibility":
            return await self._b.check_loan_eligibility(
                user_id,
                amount=Decimal(str(args["amount"])),
                term_months=int(args["term_months"]),
            )
        raise BankingError(f"unknown tool: {name}")


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(f"unserializable: {type(o)}")
