"""What each strategy holds and has done, from recorded fills and orders (never from memory)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from trading.execution.state_machine import TERMINAL
from trading.quant.models import Holding

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
            " ORDER BY a.strategy_id, i.symbol, f.ts, f.id"
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
