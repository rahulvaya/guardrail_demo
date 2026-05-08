"""SQLAlchemy ORM models for the bank schema.

All tables live in the `bank` schema, owned by `bank_user`.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    MetaData,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base bound to the `bank` schema."""

    metadata = MetaData(schema="bank")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False, default="00000")
    credit_score: Mapped[int] = mapped_column(default=700)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    accounts: Mapped[list[Account]] = relationship(back_populates="customer", cascade="all,delete-orphan")
    cards: Mapped[list[Card]] = relationship(back_populates="customer", cascade="all,delete-orphan")


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("bank.customers.id"), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(32), nullable=False)  # checking | savings
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    customer: Mapped[Customer] = relationship(back_populates="accounts")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="account",
        cascade="all,delete-orphan",
        order_by="Transaction.posted_at.desc()",
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("bank.accounts.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # positive = credit, negative = debit
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    counterparty: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    account: Mapped[Account] = relationship(back_populates="transactions")


class Card(Base):
    __tablename__ = "cards"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("bank.customers.id"), nullable=False, index=True)
    last4: Mapped[str] = mapped_column(String(4), nullable=False)
    brand: Mapped[str] = mapped_column(String(32), nullable=False, default="VISA")
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)

    customer: Mapped[Customer] = relationship(back_populates="cards")


class Atm(Base):
    __tablename__ = "atms"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str] = mapped_column(String(300), nullable=False)
    postal_code: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    latitude: Mapped[float] = mapped_column(nullable=False)
    longitude: Mapped[float] = mapped_column(nullable=False)
    open_24h: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
