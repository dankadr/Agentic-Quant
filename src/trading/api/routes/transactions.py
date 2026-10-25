"""Read-only transaction history: fills, round-trip trades, the cash ledger and daily sessions."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_session
from trading.api.schemas import Money, Qty
from trading.core.time import UtcDatetime

router = APIRouter(prefix="/v1", tags=["transactions"])
_ZERO = Decimal(0)
_FLAT = Decimal("0.0001")


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


class FillStrategy(BaseModel):
    strategy_id: str
    qty: Qty
    pnl: Money | None


class FillOut(BaseModel):
    ts: UtcDatetime
    symbol: str
    side: str
    qty: Qty
    price: Qty
    notional: Money
    fees: Money
    slippage_estimate: Money
    execution_id: str
    order_status: str
    strategies: list[FillStrategy]


class FillList(BaseModel):
    items: list[FillOut]


@router.get("/fills", response_model=FillList)
def get_fills(
    day: date | None = Query(default=None, alias="date"),
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> FillList:
    start, end = _day_bounds(day) if day is not None else (None, None)
    rows = session.execute(
        text(
            "SELECT f.id, f.ts, i.symbol, o.side, f.qty, f.price, f.fees, f.slippage_estimate,"
            " f.execution_id, o.status FROM fills f JOIN orders o ON o.id = f.order_id"
            " JOIN accounts a ON a.id = o.account_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE a.name NOT LIKE 'replay-%' AND a.name NOT LIKE 'backtest%'"
            " AND (CAST(:s AS timestamptz) IS NULL OR f.ts >= :s)"
            " AND (CAST(:e AS timestamptz) IS NULL OR f.ts < :e)"
            " ORDER BY f.ts DESC, f.id DESC LIMIT :lim"
        ),
        {"lim": limit, "s": start, "e": end},
    ).all()
    attribution: dict[Any, list[FillStrategy]] = defaultdict(list)
    if rows:
        for r in session.execute(
            text(
                "SELECT fill_id, strategy_id, allocated_qty, pnl FROM strategy_attribution"
                " WHERE fill_id = ANY(:ids) ORDER BY strategy_id"
            ),
            {"ids": [r.id for r in rows]},
        ):
            attribution[r.fill_id].append(
                FillStrategy(strategy_id=r.strategy_id, qty=abs(r.allocated_qty), pnl=r.pnl)
            )
    return FillList(
        items=[
            FillOut(
                ts=r.ts,
                symbol=r.symbol,
                side=r.side,
                qty=r.qty,
                price=r.price,
                notional=r.qty * r.price,
                fees=r.fees,
                slippage_estimate=r.slippage_estimate,
                execution_id=r.execution_id,
                order_status=r.status,
                strategies=attribution[r.id],
            )
            for r in rows
        ]
    )


class TradeOut(BaseModel):
    strategy_id: str
    symbol: str
    status: str  # CLOSED | OPEN
    opened_at: UtcDatetime
    closed_at: UtcDatetime | None
    qty: Qty
    avg_entry: Qty
    avg_exit: Qty | None
    gross_pnl: Money
    fees: Money
    net_pnl: Money
    hold_minutes: int | None


class TradeSummary(BaseModel):
    trades: int
    wins: int
    net_pnl: Money
    fees: Money


class TradeList(BaseModel):
    items: list[TradeOut]
    summary: TradeSummary


@router.get("/trades", response_model=TradeList)
def get_trades(
    day: date | None = Query(default=None, alias="date"),
    days: int = Query(default=30, ge=1, le=365),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> TradeList:
    """Round trips per (strategy, symbol, UTC day), built from the fill attribution."""
    if day is not None:
        start, end = _day_bounds(day)
    else:
        end = datetime.now(UTC) + timedelta(days=1)
        start = end - timedelta(days=days + 1)
    rows = session.execute(
        text(
            "SELECT s.strategy_id, i.symbol, f.ts, f.price, s.allocated_qty,"
            " s.allocated_fees, s.pnl FROM strategy_attribution s"
            " JOIN fills f ON f.id = s.fill_id JOIN orders o ON o.id = f.order_id"
            " JOIN accounts a ON a.id = o.account_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE a.name NOT LIKE 'replay-%' AND a.name NOT LIKE 'backtest%'"
            " AND f.ts >= :s AND f.ts < :e ORDER BY f.ts, f.id"
        ),
        {"s": start, "e": end},
    )
    groups: dict[tuple[str, str, date], list[Any]] = defaultdict(list)
    for r in rows:
        groups[(r.strategy_id, r.symbol, r.ts.astimezone(UTC).date())].append(r)
    items: list[TradeOut] = []
    for (strategy, symbol, _day), legs in groups.items():
        buys = [x for x in legs if x.allocated_qty > 0]
        sells = [x for x in legs if x.allocated_qty < 0]
        if not buys:
            continue  # an exit with no entry in this window (overnight carry) is not a round trip
        bought = sum((x.allocated_qty for x in buys), _ZERO)
        sold = sum((-x.allocated_qty for x in sells), _ZERO)
        entry = sum((x.allocated_qty * x.price for x in buys), _ZERO) / bought
        exit_ = sum((-x.allocated_qty * x.price for x in sells), _ZERO) / sold if sold > 0 else None
        net = sum((x.pnl or _ZERO for x in legs), _ZERO)
        fees = sum((x.allocated_fees for x in legs), _ZERO)
        closed = abs(bought - sold) < _FLAT
        opened_at = buys[0].ts
        closed_at = sells[-1].ts if closed and sells else None
        items.append(
            TradeOut(
                strategy_id=strategy,
                symbol=symbol,
                status="CLOSED" if closed else "OPEN",
                opened_at=opened_at,
                closed_at=closed_at,
                qty=bought,
                avg_entry=entry,
                avg_exit=exit_,
                gross_pnl=net + fees,
                fees=fees,
                net_pnl=net,
                hold_minutes=int((closed_at - opened_at).total_seconds() // 60)
                if closed_at
                else None,
            )
        )
    items.sort(key=lambda t: t.opened_at, reverse=True)
    summary = TradeSummary(
        trades=len(items),
        wins=sum(1 for t in items if t.net_pnl > 0),
        net_pnl=sum((t.net_pnl for t in items), _ZERO),
        fees=sum((t.fees for t in items), _ZERO),
    )
    return TradeList(items=items, summary=summary)


class LedgerEntry(BaseModel):
    ts: UtcDatetime
    amount: Money
    source_type: str
    source_id: str | None
    settles_on: date


class LedgerList(BaseModel):
    items: list[LedgerEntry]


@router.get("/ledger", response_model=LedgerList)
def get_ledger(
    limit: int = Query(default=100, ge=1, le=500),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> LedgerList:
    rows = session.execute(
        text(
            "SELECT l.ts, l.amount, l.source_type, l.source_id, l.settles_on"
            " FROM cash_ledger l JOIN accounts a ON a.id = l.account_id"
            " WHERE a.name NOT LIKE 'replay-%' AND a.name NOT LIKE 'backtest%'"
            " ORDER BY l.ts DESC, l.id DESC LIMIT :lim"
        ),
        {"lim": limit},
    )
    return LedgerList(
        items=[
            LedgerEntry(
                ts=r.ts,
                amount=r.amount,
                source_type=r.source_type,
                source_id=r.source_id,
                settles_on=r.settles_on,
            )
            for r in rows
        ]
    )


class SessionRow(BaseModel):
    date: date
    start_equity: Money
    end_equity: Money
    pnl: Money
    return_pct: Qty | None


class SessionList(BaseModel):
    items: list[SessionRow]


@router.get("/sessions", response_model=SessionList)
def get_sessions(
    limit: int = Query(default=60, ge=1, le=400),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> SessionList:
    """Daily start/end equity (the end of a still-running day is its latest snapshot)."""
    rows = session.execute(
        text(
            "WITH s AS (SELECT (e.ts AT TIME ZONE 'UTC')::date AS d, e.ts, e.kind, e.equity"
            " FROM equity_snapshots e JOIN accounts a ON a.id = e.account_id"
            " WHERE a.name NOT LIKE 'replay-%' AND a.name NOT LIKE 'backtest%')"
            " SELECT d,"
            " (array_agg(equity ORDER BY CASE WHEN kind = 'SESSION_START' THEN 0 ELSE 1 END,"
            " ts))[1] AS start_eq,"
            " (array_agg(equity ORDER BY CASE WHEN kind = 'SESSION_END' THEN 0 ELSE 1 END,"
            " ts DESC))[1] AS end_eq"
            " FROM s GROUP BY d ORDER BY d DESC LIMIT :lim"
        ),
        {"lim": limit},
    )
    out = []
    for r in rows:
        pnl = r.end_eq - r.start_eq
        out.append(
            SessionRow(
                date=r.d,
                start_equity=r.start_eq,
                end_equity=r.end_eq,
                pnl=pnl,
                return_pct=pnl / r.start_eq if r.start_eq else None,
            )
        )
    return SessionList(items=out)
