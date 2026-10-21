"""Market data tables: instruments, bars, quotes, snapshots, corporate actions."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import CASH, Base


class Instrument(Base):
    __tablename__ = "instruments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    symbol: Mapped[str]
    venue: Mapped[str]
    asset_type: Mapped[str]
    tick_size: Mapped[Decimal]
    lot_size: Mapped[Decimal]
    fractional_eligible: Mapped[bool]
    sector: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)


class MarketSnapshotRow(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    as_of: Mapped[datetime]
    provider: Mapped[str]
    data_hash: Mapped[str]
    quality_status: Mapped[str]
    content_json: Mapped[dict[str, Any]]


class MarketBar(Base):
    __tablename__ = "market_bars"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    ingested_at: Mapped[datetime]
    session_date: Mapped[date | None]
    source_seq: Mapped[str | None]
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class MarketQuote(Base):
    __tablename__ = "market_quotes"
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"), primary_key=True)
    ts: Mapped[datetime] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(primary_key=True)
    bid: Mapped[Decimal]
    ask: Mapped[Decimal]
    bid_size: Mapped[Decimal | None]
    ask_size: Mapped[Decimal | None]
    ingested_at: Mapped[datetime]
    quality_flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class CorporateAction(Base):
    __tablename__ = "corporate_actions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    action_type: Mapped[str]
    ex_date: Mapped[date]
    ratio_from: Mapped[Decimal | None]
    ratio_to: Mapped[Decimal | None]
    cash_amount: Mapped[Decimal | None]
    provider: Mapped[str]
    announced_at: Mapped[datetime]
    ingested_at: Mapped[datetime]


__all__ = ["CASH", "CorporateAction", "Instrument", "MarketBar", "MarketQuote", "MarketSnapshotRow"]
