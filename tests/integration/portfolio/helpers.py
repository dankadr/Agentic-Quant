"""Shared builders for ledger/valuation tests."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from tests.factories import (
    T0,
    make_instrument,
    make_intent,
    make_order,
    make_risk_decision,
    make_snapshot,
)
from trading.brokers.base import BrokerFill
from trading.core.enums import Side
from trading.db.models.market import Instrument
from trading.db.models.portfolio import Account
from trading.db.models.trading import Order
from trading.portfolio.ledger import open_paper_account

D = Decimal
DAY = date(2026, 10, 19)
NEXT_DAY = date(2026, 10, 20)


def open_account(db: Session, cash: str = "1000.00") -> UUID:
    return open_paper_account(db, name="paper-primary", starting_cash=D(cash), now=T0)


def get_instrument(db: Session, symbol: str = "SPY") -> Instrument:
    from sqlalchemy import select

    existing = db.scalars(select(Instrument).where(Instrument.symbol == symbol)).first()
    return existing or make_instrument(db, symbol)


def new_order(
    db: Session,
    account_id: UUID,
    coid: str,
    *,
    side: str = "BUY",
    qty: str = "1.0",
    limit: str | None = "100.05",
    status: str = "ACKNOWLEDGED",
    symbol: str = "SPY",
) -> Order:
    account = db.get(Account, account_id)
    assert account is not None
    inst = get_instrument(db, symbol)
    snapshot = make_snapshot(db, data_hash=f"snap-{coid}")
    decision = make_risk_decision(db, account, inst, snapshot, dedupe_key=f"dk-{coid}")
    intent = make_intent(
        db,
        account,
        inst,
        decision,
        client_order_id=coid,
        quantity=qty,
        side=side,
        limit_price=limit,
    )
    return make_order(db, intent, status=status)


def broker_fill(
    order: Order,
    execution_id: str,
    *,
    qty: str,
    price: str,
    fees: str = "0",
    symbol: str = "SPY",
) -> BrokerFill:
    return BrokerFill(
        execution_id=execution_id,
        client_order_id=order.client_order_id,
        broker_order_id=f"B-{order.client_order_id}",
        symbol=symbol,
        side=Side(order.side),
        qty=D(qty),
        price=D(price),
        fees=D(fees),
        slippage_estimate=D("0"),
        ts=T0,
    )
