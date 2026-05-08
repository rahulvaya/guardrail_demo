"""Repository layer.

Encapsulates all SQL access (Repository pattern). Keeps routers thin and
makes it easy to swap to a real backend without touching API surface.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


class CustomerRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def exists(self, customer_id: str) -> bool:
        return self.s.get(models.Customer, customer_id) is not None

    def get(self, customer_id: str) -> models.Customer | None:
        return self.s.get(models.Customer, customer_id)


class AccountRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def list_for_customer(self, customer_id: str) -> list[models.Account]:
        return list(
            self.s.execute(
                select(models.Account).where(models.Account.customer_id == customer_id)
            ).scalars()
        )

    def get_owned(self, customer_id: str, account_id: str) -> models.Account | None:
        acct = self.s.get(models.Account, account_id)
        if acct is None or acct.customer_id != customer_id:
            return None
        return acct


class TransactionRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def list_for_account(
        self,
        account_id: str,
        *,
        limit: int = 10,
        since: date | None = None,
    ) -> list[models.Transaction]:
        stmt = (
            select(models.Transaction)
            .where(models.Transaction.account_id == account_id)
            .order_by(models.Transaction.posted_at.desc())
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(models.Transaction.posted_at >= datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc))
        return list(self.s.execute(stmt).scalars())


class TransferService:
    """Atomic transfer between two accounts, both owned by the same customer."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def transfer(
        self,
        customer_id: str,
        from_account_id: str,
        to_account_id: str,
        amount: Decimal,
        memo: str | None,
    ) -> dict:
        if from_account_id == to_account_id:
            raise ValueError("source and destination accounts must differ")
        if amount <= 0:
            raise ValueError("amount must be positive")

        # Lock both rows in deterministic order to avoid deadlocks.
        ids = sorted([from_account_id, to_account_id])
        rows = self.s.execute(
            select(models.Account).where(models.Account.id.in_(ids)).with_for_update()
        ).scalars().all()
        accounts = {a.id: a for a in rows}

        src = accounts.get(from_account_id)
        dst = accounts.get(to_account_id)
        if src is None or dst is None:
            raise LookupError("account not found")
        if src.customer_id != customer_id or dst.customer_id != customer_id:
            raise PermissionError("accounts must belong to the customer")
        if src.balance < amount:
            raise ValueError("insufficient funds")

        src.balance = src.balance - amount
        dst.balance = dst.balance + amount

        now = datetime.now(timezone.utc)
        transfer_id = f"xfr-{uuid.uuid4().hex[:12]}"
        memo_text = memo or "Internal Transfer"
        self.s.add(
            models.Transaction(
                id=f"txn-{transfer_id}-out",
                account_id=src.id,
                amount=-amount,
                description=memo_text,
                counterparty=dst.id,
                posted_at=now,
            )
        )
        self.s.add(
            models.Transaction(
                id=f"txn-{transfer_id}-in",
                account_id=dst.id,
                amount=amount,
                description=memo_text,
                counterparty=src.id,
                posted_at=now,
            )
        )
        self.s.commit()

        return {
            "transfer_id": transfer_id,
            "from_account_id": src.id,
            "to_account_id": dst.id,
            "amount": amount,
            "new_from_balance": src.balance,
            "new_to_balance": dst.balance,
            "posted_at": now,
        }


class CardRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def list_for_customer(self, customer_id: str) -> list[models.Card]:
        return list(
            self.s.execute(
                select(models.Card).where(models.Card.customer_id == customer_id)
            ).scalars()
        )

    def block(self, customer_id: str, card_id: str, reason: str) -> models.Card:
        card = self.s.get(models.Card, card_id)
        if card is None or card.customer_id != customer_id:
            raise LookupError("card not found")
        card.blocked = True
        card.blocked_reason = reason
        self.s.commit()
        return card


class AtmRepository:
    """Postal-code based ATM lookup with naive haversine ordering.

    A real backend would use PostGIS or a geo index. For demo, we filter
    by exact postal-code prefix and rank by haversine distance from the
    centroid of matching ATMs.
    """

    def __init__(self, session: Session) -> None:
        self.s = session

    def find(self, postal_code: str, radius_km: float) -> list[tuple[models.Atm, float]]:
        prefix = postal_code[:3] if len(postal_code) >= 3 else postal_code
        stmt = select(models.Atm).where(models.Atm.postal_code.startswith(prefix))
        atms = list(self.s.execute(stmt).scalars())
        if not atms:
            return []

        # Use first ATM's coordinates as the anchor.
        anchor_lat, anchor_lon = atms[0].latitude, atms[0].longitude
        scored: list[tuple[models.Atm, float]] = []
        for atm in atms:
            d = _haversine_km(anchor_lat, anchor_lon, atm.latitude, atm.longitude)
            if d <= radius_km:
                scored.append((atm, d))
        scored.sort(key=lambda x: x[1])
        return scored


class LoanService:
    """Trivial deterministic loan eligibility based on credit score and DTI proxy."""

    def __init__(self, session: Session) -> None:
        self.s = session

    def evaluate(self, customer_id: str, amount: Decimal, term_months: int) -> dict:
        cust = self.s.get(models.Customer, customer_id)
        if cust is None:
            raise LookupError("customer not found")

        score = cust.credit_score
        # Max amount = score * 100, e.g. score 760 -> $76k cap
        max_amount = Decimal(score) * Decimal("100")
        if score < 600:
            return {
                "eligible": False,
                "max_amount": Decimal("0"),
                "estimated_apr": 0.0,
                "reason": "credit score below minimum",
            }
        if amount > max_amount:
            return {
                "eligible": False,
                "max_amount": max_amount,
                "estimated_apr": 0.0,
                "reason": f"amount exceeds maximum of {max_amount}",
            }
        # APR: 4% base + adjustments
        apr = 4.0 + max(0, (740 - score)) * 0.02 + (term_months / 360.0) * 2.0
        return {
            "eligible": True,
            "max_amount": max_amount,
            "estimated_apr": round(apr, 2),
            "reason": "approved",
        }


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
