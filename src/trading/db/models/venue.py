"""The paper broker's own state, kept in a separate schema as if it were an external system.

The application ledger never reads these tables except through ``PaperBroker``; reconciliation
compares the two sides, which is only meaningful because they are stored independently.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import CASH, Base

_SCHEMA = {"schema": "paper_venue"}


class VenueAccount(Base):
    __tablename__ = "venue_accounts"
    __table_args__ = _SCHEMA
    account_ref: Mapped[str] = mapped_column(primary_key=True)
    starting_cash: Mapped[Decimal] = mapped_column(CASH)


class VenueOrder(Base):
    __tablename__ = "venue_orders"
    __table_args__ = _SCHEMA
    client_order_id: Mapped[str] = mapped_column(primary_key=True)
    broker_order_id: Mapped[str] = mapped_column(unique=True)
    account_ref: Mapped[str] = mapped_column(ForeignKey("paper_venue.venue_accounts.account_ref"))
    symbol: Mapped[str]
    side: Mapped[str]
    type: Mapped[str]
    tif: Mapped[str]
    limit_price: Mapped[Decimal | None]
    qty: Mapped[Decimal]
    filled_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    status: Mapped[str]
    submitted_at: Mapped[datetime]
    eligible_at: Mapped[datetime]
    fill_seq: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[datetime]


class VenueFill(Base):
    __tablename__ = "venue_fills"
    __table_args__ = _SCHEMA
    execution_id: Mapped[str] = mapped_column(primary_key=True)
    client_order_id: Mapped[str] = mapped_column(
        ForeignKey("paper_venue.venue_orders.client_order_id")
    )
    account_ref: Mapped[str]
    symbol: Mapped[str]
    side: Mapped[str]
    ts: Mapped[datetime]
    qty: Mapped[Decimal]
    price: Mapped[Decimal]
    fees: Mapped[Decimal] = mapped_column(CASH)
    slippage_estimate: Mapped[Decimal] = mapped_column(CASH)
