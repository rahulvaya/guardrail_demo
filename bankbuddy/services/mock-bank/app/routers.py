"""HTTP routes for the mock bank.

Customer identity is taken from the `X-User-Id` header. In production a real
core-banking system would authenticate; for the mock we trust the upstream
agent (which only ever runs on the internal Docker network).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from .db import get_session
from .repositories import (
    AccountRepository,
    AtmRepository,
    CardRepository,
    CustomerRepository,
    LoanService,
    TransactionRepository,
    TransferService,
)
from .schemas import (
    AccountOut,
    AtmOut,
    BlockCardIn,
    CardOut,
    LoanEligibilityIn,
    LoanEligibilityOut,
    TransactionOut,
    TransferIn,
    TransferOut,
)

router = APIRouter()


def _user_id(x_user_id: str | None = Header(default=None, alias="X-User-Id")) -> str:
    if not x_user_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-User-Id header required")
    return x_user_id


def _require_customer(session: Session, customer_id: str) -> None:
    if not CustomerRepository(session).exists(customer_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"customer {customer_id} not found")


@router.get("/accounts", response_model=list[AccountOut])
def list_accounts(
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> list[AccountOut]:
    _require_customer(session, user_id)
    return [AccountOut.model_validate(a, from_attributes=True) for a in AccountRepository(session).list_for_customer(user_id)]


@router.get("/accounts/{account_id}/transactions", response_model=list[TransactionOut])
def list_transactions(
    account_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    since: date | None = Query(default=None),
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> list[TransactionOut]:
    acct = AccountRepository(session).get_owned(user_id, account_id)
    if acct is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "account not found")
    rows = TransactionRepository(session).list_for_account(account_id, limit=limit, since=since)
    return [TransactionOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("/transfers", response_model=TransferOut)
def create_transfer(
    body: TransferIn,
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> TransferOut:
    _require_customer(session, user_id)
    try:
        out = TransferService(session).transfer(
            customer_id=user_id,
            from_account_id=body.from_account_id,
            to_account_id=body.to_account_id,
            amount=body.amount,
            memo=body.memo,
        )
    except PermissionError as e:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    return TransferOut(**out)


@router.get("/cards", response_model=list[CardOut])
def list_cards(
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> list[CardOut]:
    _require_customer(session, user_id)
    rows = CardRepository(session).list_for_customer(user_id)
    return [CardOut.model_validate(c, from_attributes=True) for c in rows]


@router.post("/cards/{card_id}/block", response_model=CardOut)
def block_card(
    card_id: str,
    body: BlockCardIn,
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> CardOut:
    _require_customer(session, user_id)
    try:
        card = CardRepository(session).block(user_id, card_id, body.reason)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return CardOut.model_validate(card, from_attributes=True)


@router.get("/atms", response_model=list[AtmOut])
def find_atms(
    postal_code: str = Query(min_length=3, max_length=20),
    radius_km: float = Query(default=5.0, gt=0, le=200),
    session: Session = Depends(get_session),
) -> list[AtmOut]:
    pairs = AtmRepository(session).find(postal_code, radius_km)
    out: list[AtmOut] = []
    for atm, dist in pairs:
        item = AtmOut.model_validate(atm, from_attributes=True)
        item.distance_km = round(dist, 2)
        out.append(item)
    return out


@router.post("/loans/eligibility", response_model=LoanEligibilityOut)
def loan_eligibility(
    body: LoanEligibilityIn,
    user_id: str = Depends(_user_id),
    session: Session = Depends(get_session),
) -> LoanEligibilityOut:
    try:
        result = LoanService(session).evaluate(user_id, body.amount, body.term_months)
    except LookupError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return LoanEligibilityOut(**result)
