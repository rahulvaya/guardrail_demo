"""Idempotent seed data for the mock bank.

Two demo customers, accounts, transactions, cards, and a handful of ATMs.
The user_id used by the API/agent maps 1:1 to `Customer.id` (string).
"""
from __future__ import annotations

import logging
import random
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from . import models

log = logging.getLogger(__name__)

# Stable demo IDs - referenced by the local-dev auth provider's fake principals.
DEMO_CUSTOMERS = [
    {
        "id": "cust-alice",
        "full_name": "Alice Anderson",
        "email": "alice@example.com",
        "postal_code": "10001",
        "credit_score": 760,
    },
    {
        "id": "cust-bob",
        "full_name": "Bob Brown",
        "email": "bob@example.com",
        "postal_code": "94016",
        "credit_score": 640,
    },
]

DEMO_ATMS = [
    ("atm-001", "Midtown Branch ATM", "100 5th Ave", "10001", 40.7411, -73.9897),
    ("atm-002", "Penn Station ATM", "1 Penn Plaza", "10001", 40.7506, -73.9935),
    ("atm-003", "SoMa ATM", "850 Mission St", "94103", 37.7836, -122.4060),
    ("atm-004", "Daly City ATM", "1 Junipero Serra Blvd", "94014", 37.6800, -122.4660),
    ("atm-005", "SFO Terminal 2 ATM", "SFO International Airport", "94128", 37.6213, -122.3790),
]


def _seed_transactions(session: Session, account_id: str, count: int = 12) -> None:
    rng = random.Random(account_id)  # deterministic per account
    descriptions = [
        ("Coffee Shop", "Blue Bottle Coffee"),
        ("Grocery", "Whole Foods Market"),
        ("Salary", "Acme Corp Payroll"),
        ("Utility", "Pacific Gas & Electric"),
        ("Transfer", "Internal Transfer"),
        ("Dining", "Local Bistro"),
        ("Subscription", "Streaming Service"),
        ("Refund", "Online Retailer"),
    ]
    now = datetime.now(timezone.utc)
    for i in range(count):
        desc, party = rng.choice(descriptions)
        is_credit = desc in ("Salary", "Refund")
        amount = Decimal(str(round(rng.uniform(5, 2500), 2)))
        if not is_credit:
            amount = -amount
        session.add(
            models.Transaction(
                id=f"txn-{account_id}-{i:03d}",
                account_id=account_id,
                amount=amount,
                description=desc,
                counterparty=party,
                posted_at=now - timedelta(days=i, hours=rng.randint(0, 23)),
            )
        )


def seed(session: Session) -> None:
    """Insert demo data if the database is empty."""
    if session.query(models.Customer).count() > 0:
        log.info("seed: customers already present, skipping")
        return

    log.info("seed: inserting demo data")

    for c in DEMO_CUSTOMERS:
        session.add(models.Customer(**c))
    session.flush()

    # Accounts + transactions
    account_specs = [
        ("acct-alice-chk", "cust-alice", "checking", Decimal("4250.75")),
        ("acct-alice-sav", "cust-alice", "savings", Decimal("18900.00")),
        ("acct-bob-chk", "cust-bob", "checking", Decimal("820.30")),
        ("acct-bob-sav", "cust-bob", "savings", Decimal("3200.00")),
    ]
    for acct_id, cust_id, kind, balance in account_specs:
        session.add(
            models.Account(
                id=acct_id,
                customer_id=cust_id,
                account_type=kind,
                currency="USD",
                balance=balance,
            )
        )
    session.flush()

    for acct_id, *_ in account_specs:
        _seed_transactions(session, acct_id)

    # Cards
    cards = [
        ("card-alice-1", "cust-alice", "4242", "VISA", date.today() + timedelta(days=730)),
        ("card-bob-1", "cust-bob", "1881", "MASTERCARD", date.today() + timedelta(days=365)),
    ]
    for card_id, cust_id, last4, brand, exp in cards:
        session.add(
            models.Card(
                id=card_id,
                customer_id=cust_id,
                last4=last4,
                brand=brand,
                expires_on=exp,
            )
        )

    # ATMs
    for atm_id, name, addr, postal, lat, lon in DEMO_ATMS:
        session.add(
            models.Atm(
                id=atm_id,
                name=name,
                address=addr,
                postal_code=postal,
                latitude=lat,
                longitude=lon,
                open_24h=True,
            )
        )

    session.commit()
    log.info("seed: done")
