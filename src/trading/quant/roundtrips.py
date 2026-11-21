"""Flat-to-flat attributed trading episodes, reconstructed from durable fills.

An exit split into several fills is one trade. Entry fees belong to that trade too.
Open episodes are deliberately excluded from closed-trade and shadow P&L statistics.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.portfolio.adjustments import applied_splits, dividend_allocations, split_factor

_ZERO = Decimal(0)
_DUST = Decimal("0.0001")


@dataclass(frozen=True)
class AttributedFill:
    strategy_id: str
    symbol: str
    ts: datetime
    qty: Decimal
    pnl: Decimal
    decision_ts: datetime | None = None


@dataclass(frozen=True)
class Roundtrip:
    strategy_id: str
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    pnl: Decimal
    decision_ts: datetime


def roundtrips(fills: Sequence[AttributedFill]) -> list[Roundtrip]:
    # Callers supply timestamp order, including a stable fill-id tie-break from the DB.
    active: dict[tuple[str, str], tuple[datetime, datetime, Decimal, Decimal]] = {}
    closed: list[Roundtrip] = []
    for fill in fills:
        key = fill.strategy_id, fill.symbol
        entry, decision, qty, pnl = active.get(
            key, (fill.ts, fill.decision_ts or fill.ts, _ZERO, _ZERO)
        )
        if fill.qty == 0 and key not in active:
            continue
        qty += fill.qty
        pnl += fill.pnl
        if abs(qty) < _DUST:
            closed.append(Roundtrip(*key, entry, fill.ts, pnl, decision))
            active.pop(key, None)
        else:
            active[key] = entry, decision, qty, pnl
    return closed


def load_roundtrips(session: Session, account_id: UUID, *, end: datetime) -> list[Roundtrip]:
    rows = session.execute(
        text(
            "SELECT a.strategy_id, i.symbol, f.ts, a.allocated_qty AS qty, a.pnl,"
            " n.created_at AS decision_ts"
            " FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
            " JOIN orders o ON o.id = f.order_id JOIN order_intents n ON n.id = o.intent_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE o.account_id = :acct AND f.ts < :end"
            " ORDER BY f.ts, f.execution_id, a.strategy_id, a.id"
        ),
        {"acct": account_id, "end": end},
    )
    splits = applied_splits(session, account_id, end)
    fills = [
        AttributedFill(
            r.strategy_id,
            r.symbol,
            r.ts,
            r.qty * split_factor(splits, r.symbol, r.ts),
            r.pnl or _ZERO,
            r.decision_ts,
        )
        for r in rows
    ]
    fills.extend(
        AttributedFill(owner, symbol, ts, _ZERO, pnl)
        for ts, symbol, owner, pnl in dividend_allocations(session, account_id, end)
    )
    return roundtrips(sorted(fills, key=lambda f: (f.ts, f.qty != 0)))
