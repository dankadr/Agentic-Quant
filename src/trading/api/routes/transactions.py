"""Read-only transaction history: fills, round-trip trades, the cash ledger and daily sessions."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_clock, get_session
from trading.api.schemas import Direction, Money, Qty, TradeActionOut, action_label
from trading.core.clock import Clock
from trading.core.enums import PositionEffect, Side, effective_effect
from trading.core.time import UtcDatetime
from trading.db.models.portfolio import Account
from trading.quant.roundtrips import load_trade_episodes

router = APIRouter(prefix="/v1", tags=["transactions"])
_ZERO = Decimal(0)


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


class FillStrategy(BaseModel):
    strategy_id: str
    qty: Qty
    pnl: Money | None


class FillOut(TradeActionOut):
    ts: UtcDatetime
    symbol: str
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
            " f.execution_id, o.status, o.position_effect"
            " FROM fills f JOIN orders o ON o.id = f.order_id"
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
                position_effect=effective_effect(Side(r.side), r.position_effect),
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
    direction: Direction
    opening_action: str
    closing_action: str


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
    clock: Clock = Depends(get_clock),
) -> TradeList:
    """Attributed episodes opened or closed in the requested UTC window."""
    if day is not None:
        start, end = _day_bounds(day)
    else:
        now = clock.now()
        start, end = now - timedelta(days=days), now + timedelta(microseconds=1)
    account_ids = session.scalars(
        select(Account.id).where(
            Account.name.not_like("replay-%"), Account.name.not_like("backtest%")
        )
    )
    items: list[TradeOut] = []
    for account_id in account_ids:
        for episode in load_trade_episodes(session, account_id, end=end):
            closed_at = episode.exit_ts
            if (closed_at or episode.entry_ts) < start:
                continue
            opened_at = episode.entry_ts
            opening = PositionEffect.OPEN_SHORT if episode.short else PositionEffect.OPEN_LONG
            closing = PositionEffect.CLOSE_SHORT if episode.short else PositionEffect.CLOSE_LONG
            items.append(
                TradeOut(
                    strategy_id=episode.strategy_id,
                    symbol=episode.symbol,
                    status="CLOSED" if closed_at else "OPEN",
                    opened_at=opened_at,
                    closed_at=closed_at,
                    qty=episode.entry_qty,
                    avg_entry=episode.entry_notional / episode.entry_qty,
                    avg_exit=episode.exit_notional / episode.exit_qty if episode.exit_qty else None,
                    gross_pnl=episode.pnl + episode.fees,
                    fees=episode.fees,
                    net_pnl=episode.pnl,
                    hold_minutes=int((closed_at - opened_at).total_seconds() // 60)
                    if closed_at
                    else None,
                    direction="SHORT" if episode.short else "LONG",
                    opening_action=action_label(opening),
                    closing_action=action_label(closing),
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
