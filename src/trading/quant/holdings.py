"""What each strategy holds and has done, from recorded fills and orders (never from memory)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from trading.execution.state_machine import TERMINAL
from trading.quant.models import EntryHistory, Holding

_TERMINAL = tuple(s.value for s in TERMINAL)


# Anything below the smallest tradable increment is rounding residue, not a position.
DUST = Decimal("0.0001")


@dataclass
class _Lot:
    opened_at: datetime
    qty: Decimal = Decimal(0)
    bought: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)


def load_holdings(session: Session, account_id: UUID, as_of: datetime) -> tuple[Holding, ...]:
    """Open lots per (strategy, symbol), replayed from fill attribution up to ``as_of``.

    A lot starts at the first buy after the strategy was last flat, so yesterday's closed round
    trips never age or re-price today's entry.
    """
    rows = session.execute(
        text(
            "SELECT a.strategy_id, i.symbol, a.allocated_qty AS qty, f.price, f.ts"
            " FROM strategy_attribution a"
            " JOIN fills f ON f.id = a.fill_id"
            " JOIN orders o ON o.id = f.order_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE o.account_id = :acct AND f.ts <= :as_of"
            " ORDER BY a.strategy_id, i.symbol, f.ts, f.execution_id"
        ),
        {"acct": account_id, "as_of": as_of},
    ).all()
    lots: dict[tuple[str, str], _Lot] = {}
    for r in rows:
        qty = Decimal(r.qty)
        lot = lots.setdefault((r.strategy_id, r.symbol), _Lot(r.ts))
        if qty > 0:
            if lot.qty < DUST:  # flat before this buy: a new lot
                lot = lots[(r.strategy_id, r.symbol)] = _Lot(r.ts)
            lot.bought += qty
            lot.cost += qty * Decimal(r.price)
        lot.qty += qty
    return tuple(
        Holding(strategy, symbol, lot.qty, lot.cost / lot.bought, lot.opened_at)
        for (strategy, symbol), lot in sorted(lots.items())
        if lot.qty >= DUST and lot.bought > 0
    )


def load_entered_today(
    session: Session, account_id: UUID, since: datetime
) -> frozenset[tuple[str, str]]:
    """(strategy, symbol) pairs with a BUY that is open or filled this session (rejected ones
    never happened). Enforces one entry per symbol per strategy per session."""
    rows = session.execute(
        text(
            "SELECT DISTINCT s.strategy_id, i.symbol FROM order_intents n"
            " JOIN orders o ON o.intent_id = n.id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " CROSS JOIN LATERAL unnest(n.strategy_ids) AS s(strategy_id)"
            " WHERE o.account_id = :acct AND n.side = 'BUY' AND n.created_at >= :since"
            " AND (o.status NOT IN ('REJECTED', 'CANCELED', 'EXPIRED') OR o.filled_qty > 0)"
        ),
        {"acct": account_id, "since": since},
    ).all()
    return frozenset((r.strategy_id, r.symbol) for r in rows)


def load_in_flight(session: Session, account_id: UUID) -> frozenset[tuple[str, str, str]]:
    """(strategy, symbol, side) with a non-terminal order: do not stack another on top."""
    rows = session.execute(
        text(
            "SELECT DISTINCT s.strategy_id, i.symbol, o.side FROM orders o"
            " JOIN order_intents n ON n.id = o.intent_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " CROSS JOIN LATERAL unnest(n.strategy_ids) AS s(strategy_id)"
            " WHERE o.account_id = :acct AND o.status NOT IN :terminal"
        ).bindparams(bindparam("terminal", expanding=True)),
        {"acct": account_id, "terminal": list(_TERMINAL)},
    ).all()
    return frozenset((r.strategy_id, r.symbol, r.side) for r in rows)


def load_open_buy_by_strategy(session: Session, account_id: UUID) -> dict[str, Decimal]:
    """Unfilled BUY notional resting per strategy (split equally across a netted order's owners)."""
    rows = session.execute(
        text(
            "SELECT s.strategy_id,"
            " sum((o.qty - o.filled_qty) * coalesce(o.limit_price, 0)"
            " / cardinality(n.strategy_ids)) AS notional"
            " FROM orders o JOIN order_intents n ON n.id = o.intent_id"
            " CROSS JOIN LATERAL unnest(n.strategy_ids) AS s(strategy_id)"
            " WHERE o.account_id = :acct AND o.side = 'BUY' AND o.status NOT IN :terminal"
            " GROUP BY s.strategy_id"
        ).bindparams(bindparam("terminal", expanding=True)),
        {"acct": account_id, "terminal": list(_TERMINAL)},
    ).all()
    return {r.strategy_id: Decimal(r.notional) for r in rows}


def load_entry_history(
    session: Session, account_id: UUID, since: datetime, as_of: datetime
) -> dict[tuple[str, str], EntryHistory]:
    """Reconstruct flat-to-long entries and fully-flat exits, including partial fills.

    Replay earlier fills too: overnight holdings must not be mistaken for new session entries.
    Pending orders reserve an entry slot without consuming the filled-entry budget.
    """
    rows = session.execute(
        text(
            "SELECT a.strategy_id, i.symbol, a.allocated_qty AS qty, f.ts"
            " FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
            " JOIN orders o ON o.id = f.order_id JOIN instruments i ON i.id = o.instrument_id"
            " WHERE o.account_id = :acct AND f.ts <= :at"
            " ORDER BY f.ts, f.execution_id, a.strategy_id"
        ),
        {"acct": account_id, "at": as_of},
    )
    qtys: dict[tuple[str, str], Decimal] = {}
    histories: dict[tuple[str, str], EntryHistory] = {}
    for row in rows:
        key = (row.strategy_id, row.symbol)
        before = qtys.get(key, Decimal(0))
        qty = Decimal(row.qty)
        after = before + qty
        qtys[key] = after
        h = histories.get(key, EntryHistory())
        if qty > 0 and before < DUST and after >= DUST:
            if row.ts >= since:
                h = EntryHistory(h.entries + 1, None)
        elif before >= DUST and after < DUST and row.ts >= since:
            h = EntryHistory(h.entries, row.ts)
        # Do not carry yesterday's closed episodes into today's limits or cooldown.
        if h.entries or h.last_exit_at is not None:
            histories[key] = h
    pending = session.execute(
        text(
            "SELECT DISTINCT s.strategy_id, i.symbol FROM orders o"
            " JOIN order_intents n ON n.id=o.intent_id"
            " JOIN instruments i ON i.id=o.instrument_id"
            " CROSS JOIN LATERAL unnest(n.strategy_ids) AS s(strategy_id)"
            " WHERE o.account_id=:acct AND o.created_at <= :at"
            " AND o.status NOT IN :terminal"
        ).bindparams(bindparam("terminal", expanding=True)),
        {"acct": account_id, "at": as_of, "terminal": list(_TERMINAL)},
    )
    for row in pending:
        key = (row.strategy_id, row.symbol)
        h = histories.get(key, EntryHistory())
        histories[key] = EntryHistory(h.entries, h.last_exit_at, True)
    return histories
