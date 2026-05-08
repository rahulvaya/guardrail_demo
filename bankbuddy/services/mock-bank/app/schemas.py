"""Pydantic DTOs for the mock-bank HTTP API."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class AccountOut(BaseModel):
    id: str
    customer_id: str
    account_type: str
    currency: str
    balance: Decimal


class TransactionOut(BaseModel):
    id: str
    account_id: str
    amount: Decimal
    description: str
    counterparty: str
    posted_at: datetime


class TransferIn(BaseModel):
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(gt=0)
    memo: str | None = None


class TransferOut(BaseModel):
    transfer_id: str
    from_account_id: str
    to_account_id: str
    amount: Decimal
    new_from_balance: Decimal
    new_to_balance: Decimal
    posted_at: datetime


class CardOut(BaseModel):
    id: str
    customer_id: str
    last4: str
    brand: str
    blocked: bool
    blocked_reason: str | None
    expires_on: date


class BlockCardIn(BaseModel):
    reason: str = Field(min_length=2, max_length=200)


class AtmOut(BaseModel):
    id: str
    name: str
    address: str
    postal_code: str
    latitude: float
    longitude: float
    open_24h: bool
    distance_km: float | None = None


class LoanEligibilityIn(BaseModel):
    amount: Decimal = Field(gt=0)
    term_months: int = Field(ge=3, le=360)


class LoanEligibilityOut(BaseModel):
    eligible: bool
    max_amount: Decimal
    estimated_apr: float
    reason: str
