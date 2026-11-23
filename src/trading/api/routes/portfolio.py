"""Read-only portfolio, positions and orders."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_clock, get_config, get_session
from trading.api.pagination import decode_cursor, encode_cursor
from trading.api.problems import ApiProblem
from trading.api.schemas import Direction, Money, Qty, TradeActionOut
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.enums import Side, effective_effect
from trading.core.time import UtcDatetime
from trading.db.models.market import Instrument
from trading.db.models.portfolio import Account, EquitySnapshot, Position
from trading.db.models.trading import FillRow, Order
from trading.execution.state_machine import TERMINAL, OrderStatus
from trading.portfolio.adjustments import applied_splits, split_factor
from trading.portfolio.balances import MissingMark, reserved_for_open_buys
from trading.portfolio.valuation import short_reserves

router = APIRouter(prefix="/v1", tags=["portfolio"])
_ZERO = Decimal(0)


class PortfolioOut(BaseModel):
    as_of: UtcDatetime
    equity: Money
    cash: Money
    buying_power: Money
    realized_pnl: Money
    unrealized_pnl: Money
    exposure: Money
    currency: str
    short_liability: Money
    short_collateral: Money
    restricted_short_proceeds: Money


class PositionOut(BaseModel):
    symbol: str
    qty: Qty
    average_cost: Qty
    mark: Qty | None
    market_value: Qty | None
    unrealized_pnl: Qty | None
    strategy_ids: list[str]
    direction: Direction
    signed_qty: Qty
    size: Qty
    realized_pnl: Money
    short_liability: Money | None
    short_collateral: Money | None
    restricted_short_proceeds: Money


class PositionPage(BaseModel):
    items: list[PositionOut]
    next_cursor: str | None


class FillOut(BaseModel):
    execution_id: str
    ts: UtcDatetime
    qty: Qty
    price: Qty
    fees: Money


class OrderOut(TradeActionOut):
    id: str
    client_order_id: str
    broker_order_id: str | None
    symbol: str
    type: str
    limit_price: Qty | None
    qty: Qty
    filled_qty: Qty
    avg_fill_price: Qty | None
    status: str
    status_reason: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    fills: list[FillOut]


class OrderPage(BaseModel):
    items: list[OrderOut]
    next_cursor: str | None


def _latest_snapshot(session: Session) -> EquitySnapshot | None:
    return session.scalars(
        select(EquitySnapshot)
        .join(Account, Account.id == EquitySnapshot.account_id)
        .where(Account.name.not_like("replay-%"), Account.name.not_like("backtest%"))
        .order_by(EquitySnapshot.ts.desc())
        .limit(1)
    ).first()


def _reservation_asks(session: Session, account_id: UUID, now: datetime) -> dict[str, Decimal]:
    """Marks for pending orders, using each intent's original market-data provider."""
    rows = session.execute(
        text(
            "SELECT DISTINCT ON (i.symbol) i.symbol, q.ask, q.ts"
            " FROM orders o JOIN instruments i ON i.id = o.instrument_id"
            " JOIN order_intents n ON n.id = o.intent_id"
            " JOIN market_snapshots m ON m.id = n.market_snapshot_id"
            " CROSS JOIN LATERAL (SELECT ask, ts FROM market_quotes"
            "   WHERE instrument_id = o.instrument_id AND provider = m.provider"
            "   AND ts <= :now AND ingested_at <= :now AND ask > 0"
            "   ORDER BY ts DESC LIMIT 1) q"
            " WHERE o.account_id = :account AND NOT (o.status = ANY(:terminal))"
            " AND (o.limit_price IS NULL OR o.position_effect = 'OPEN_SHORT')"
            " ORDER BY i.symbol, q.ts DESC, m.provider"
        ),
        {"account": account_id, "now": now, "terminal": [s.value for s in TERMINAL]},
    ).all()
    if not rows:
        return {}
    splits = applied_splits(session, account_id, now)
    return {symbol: ask / split_factor(splits, symbol, ts) for symbol, ask, ts in rows}


def _unavailable(detail: str) -> ApiProblem:
    return ApiProblem(503, "STATE_UNAVAILABLE", "Portfolio state unavailable", detail=detail)


@router.get("/portfolio", response_model=PortfolioOut)
def get_portfolio(
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    config: AppConfig = Depends(get_config),
    clock: Clock = Depends(get_clock),
) -> PortfolioOut:
    snap = _latest_snapshot(session)
    if snap is None:
        raise _unavailable("no equity snapshot has been recorded yet")
    try:
        asks = _reservation_asks(session, snap.account_id, clock.now())
        reserved = reserved_for_open_buys(
            session, snap.account_id, ask_by_symbol=asks, cfg=config.paper_broker
        )
    except MissingMark as exc:
        raise _unavailable(str(exc)) from exc
    liability = collateral = proceeds = _ZERO
    for position in snap.positions_json.values():
        qty = Decimal(position["qty"])
        if qty < 0:
            short_value = -Decimal(position["market_value"])
            short_proceeds, short_collateral = short_reserves(
                qty, Decimal(position["average_cost"]), short_value
            )
            liability += short_value
            collateral += short_collateral
            proceeds += short_proceeds
    return PortfolioOut(
        as_of=snap.ts,
        equity=snap.equity,
        cash=snap.cash,
        buying_power=snap.settled_cash - reserved - collateral - proceeds,
        realized_pnl=snap.realized_pnl,
        unrealized_pnl=snap.unrealized_pnl,
        exposure=snap.exposure,
        currency="USD",
        short_liability=liability,
        short_collateral=collateral,
        restricted_short_proceeds=proceeds,
    )


@router.get("/positions", response_model=PositionPage)
def get_positions(
    strategy_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> PositionPage:
    snap = _latest_snapshot(session)
    stmt = (
        select(Position, Instrument.symbol)
        .join(Instrument, Instrument.id == Position.instrument_id)
        .join(Account, Account.id == Position.account_id)
        .where(
            Position.qty != 0,
            Account.name.not_like("replay-%"),
            Account.name.not_like("backtest%"),
        )
        .order_by(Position.updated_at.desc(), Position.instrument_id.desc())
    )
    if cursor:
        stmt = stmt.where(
            tuple_(Position.updated_at, Position.instrument_id) < tuple_(*decode_cursor(cursor))
        )
    rows = session.execute(stmt).all()
    items: list[tuple[Position, str, list[str]]] = []
    for position, symbol in rows:
        strategies = sorted(
            session.execute(
                text(
                    "SELECT DISTINCT unnest(i.strategy_ids) FROM fills f"
                    " JOIN orders o ON o.id = f.order_id"
                    " JOIN order_intents i ON i.id = o.intent_id"
                    " WHERE o.account_id = :a AND o.instrument_id = :n"
                ),
                {"a": position.account_id, "n": position.instrument_id},
            ).scalars()
        )
        if strategy_id is None or strategy_id in strategies:
            items.append((position, symbol, strategies))
    page = items[:limit]
    out = []
    for position, symbol, strategies in page:
        mark: dict[str, Any] = (
            snap.positions_json.get(symbol, {})
            if snap is not None and snap.account_id == position.account_id
            else {}
        )
        is_short = position.qty < 0
        if mark and (
            (Decimal(mark["qty"]) < 0) != is_short
            or (
                is_short
                and (
                    Decimal(mark["qty"]) != position.qty
                    or Decimal(mark["average_cost"]) != position.average_cost
                    or (snap is not None and snap.ts < position.updated_at)
                )
            )
        ):
            mark = {}  # A pre-fill or pre-split snapshot cannot value the current short.
        market_value = Decimal(mark["market_value"]) if mark else None
        liability: Decimal | None = _ZERO
        collateral: Decimal | None = _ZERO
        proceeds = _ZERO
        if is_short:
            liability = -market_value if market_value is not None else None
            proceeds, reserve = short_reserves(
                position.qty, position.average_cost, liability or _ZERO
            )
            collateral = reserve if liability is not None else None
        out.append(
            PositionOut(
                symbol=symbol,
                qty=position.qty,
                signed_qty=position.qty,
                size=abs(position.qty),
                direction="SHORT" if is_short else "LONG",
                average_cost=position.average_cost,
                mark=Decimal(mark["mark"]) if mark else None,
                market_value=market_value,
                unrealized_pnl=Decimal(mark["unrealized_pnl"]) if mark else None,
                realized_pnl=position.realized_pnl,
                short_liability=liability,
                short_collateral=collateral,
                restricted_short_proceeds=proceeds,
                strategy_ids=strategies,
            )
        )
    last = page[-1][0] if len(items) > limit else None
    return PositionPage(
        items=out,
        next_cursor=encode_cursor(last.updated_at, last.instrument_id) if last else None,
    )


@router.get("/orders", response_model=OrderPage)
def get_orders(
    status: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> OrderPage:
    if status is not None and status not in {s.value for s in OrderStatus}:
        raise ApiProblem(422, "INVALID_STATUS", "Unknown order status", detail=status)
    stmt = (
        select(Order, Instrument.symbol)
        .join(Instrument, Instrument.id == Order.instrument_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit + 1)
    )
    if status is not None:
        stmt = stmt.where(Order.status == status)
    if cursor:
        stmt = stmt.where(tuple_(Order.created_at, Order.id) < tuple_(*decode_cursor(cursor)))
    rows = session.execute(stmt).all()
    page = rows[:limit]
    fills: dict[UUID, list[FillRow]] = defaultdict(list)
    if page:
        for fill in session.scalars(
            select(FillRow)
            .where(FillRow.order_id.in_([o.id for o, _ in page]))
            .order_by(FillRow.ts, FillRow.execution_id)
        ):
            fills[fill.order_id].append(fill)
    items = [
        OrderOut(
            id=str(o.id),
            client_order_id=o.client_order_id,
            broker_order_id=o.broker_order_id,
            symbol=symbol,
            side=o.side,
            position_effect=effective_effect(Side(o.side), o.position_effect),
            type=o.type,
            limit_price=o.limit_price,
            qty=o.qty,
            filled_qty=o.filled_qty,
            avg_fill_price=o.avg_fill_price,
            status=o.status,
            status_reason=o.status_reason,
            created_at=o.created_at,
            updated_at=o.updated_at,
            fills=[
                FillOut(execution_id=f.execution_id, ts=f.ts, qty=f.qty, price=f.price, fees=f.fees)
                for f in fills[o.id]
            ],
        )
        for o, symbol in page
    ]
    last = page[-1][0] if len(rows) > limit else None
    return OrderPage(
        items=items,
        next_cursor=encode_cursor(last.created_at, last.id) if last else None,
    )


__all__ = ["datetime", "router"]
