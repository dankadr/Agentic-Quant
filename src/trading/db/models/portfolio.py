"""Accounts, ledger, positions, equity snapshots, sessions, reconciliation, attribution."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import CASH, Base


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    broker: Mapped[str]
    mode: Mapped[str]
    currency: Mapped[str]
    status: Mapped[str]
    peak_equity: Mapped[Decimal] = mapped_column(CASH)
    created_at: Mapped[datetime]


class CashLedgerEntry(Base):
    __tablename__ = "cash_ledger"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    ts: Mapped[datetime]
    amount: Mapped[Decimal] = mapped_column(CASH)
    currency: Mapped[str]
    source_type: Mapped[str]
    source_id: Mapped[str | None]
    settles_on: Mapped[date]
    reverses_id: Mapped[UUID | None] = mapped_column(ForeignKey("cash_ledger.id"))


class Position(Base):
    __tablename__ = "positions"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    qty: Mapped[Decimal]
    average_cost: Mapped[Decimal]
    realized_pnl: Mapped[Decimal] = mapped_column(CASH, default=Decimal(0))
    updated_at: Mapped[datetime]


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    ts: Mapped[datetime]
    kind: Mapped[str]
    cash: Mapped[Decimal] = mapped_column(CASH)
    settled_cash: Mapped[Decimal] = mapped_column(CASH)
    equity: Mapped[Decimal] = mapped_column(CASH)
    exposure: Mapped[Decimal] = mapped_column(CASH)
    realized_pnl: Mapped[Decimal] = mapped_column(CASH)
    unrealized_pnl: Mapped[Decimal] = mapped_column(CASH)
    positions_json: Mapped[dict[str, Any]]


class TradingSession(Base):
    __tablename__ = "trading_sessions"
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    session_date: Mapped[date] = mapped_column(primary_key=True)
    start_equity: Mapped[Decimal] = mapped_column(CASH)
    new_entries: Mapped[int] = mapped_column(default=0)
    daily_loss_halted_at: Mapped[datetime | None]


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    status: Mapped[str]
    report_json: Mapped[dict[str, Any]]


class StrategyAttribution(Base):
    __tablename__ = "strategy_attribution"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    fill_id: Mapped[UUID] = mapped_column(ForeignKey("fills.id"))
    allocated_qty: Mapped[Decimal]
    allocated_fees: Mapped[Decimal] = mapped_column(CASH)
    pnl: Mapped[Decimal | None] = mapped_column(CASH)
