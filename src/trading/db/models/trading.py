"""Risk decisions, order intents, orders, order events and fills."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, ForeignKey, Identity
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import CASH, Base


class RiskDecisionRow(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    signal_id: Mapped[UUID | None] = mapped_column(ForeignKey("signals.id"))
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    ts: Mapped[datetime]
    allowed: Mapped[bool]
    reasons_json: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    limits_version: Mapped[str]
    market_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("market_snapshots.id"))
    portfolio_snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("equity_snapshots.id"))
    dedupe_key: Mapped[str]
    proposal_json: Mapped[dict[str, Any]]


class OrderIntent(Base):
    __tablename__ = "order_intents"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    side: Mapped[str]
    quantity: Mapped[Decimal]
    order_type: Mapped[str]
    limit_price: Mapped[Decimal | None]
    time_in_force: Mapped[str]
    strategy_ids: Mapped[list[str]]
    market_snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("market_snapshots.id"))
    risk_decision_id: Mapped[UUID] = mapped_column(ForeignKey("risk_decisions.id"), unique=True)
    client_order_id: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime | None]
    is_entry: Mapped[bool]
    created_at: Mapped[datetime]


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id"))
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("order_intents.id"), unique=True)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    client_order_id: Mapped[str] = mapped_column(unique=True)
    broker_order_id: Mapped[str | None]
    side: Mapped[str]
    type: Mapped[str]
    time_in_force: Mapped[str]
    limit_price: Mapped[Decimal | None]
    qty: Mapped[Decimal]
    filled_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    avg_fill_price: Mapped[Decimal | None]
    status: Mapped[str]
    status_reason: Mapped[str | None]
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class OrderEvent(Base):
    __tablename__ = "order_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    # Strict insertion order: timestamps can tie, this cannot.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"))
    ts: Mapped[datetime]
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    reason: Mapped[str | None]
    payload_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class FillRow(Base):
    __tablename__ = "fills"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id"))
    execution_id: Mapped[str] = mapped_column(unique=True)
    ts: Mapped[datetime]
    qty: Mapped[Decimal]
    price: Mapped[Decimal]
    fees: Mapped[Decimal] = mapped_column(CASH)
    slippage_estimate: Mapped[Decimal] = mapped_column(CASH)
