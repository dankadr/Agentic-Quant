"""What each strategy holds and has done, from recorded fills and orders (never from memory)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from trading.execution.state_machine import TERMINAL
from trading.quant.models import Holding

_TERMINAL = tuple(s.value for s in TERMINAL)


def load_holdings(session: Session, account_id: UUID, as_of: datetime) -> tuple[Holding, ...]:
    """Open positions per (strategy, symbol), from fill attribution up to ``as_of``."""
    rows = session.execute(
        text(
            "SELECT a.strategy_id, i.symbol, sum(a.allocated_qty) AS qty,"
            " sum(CASE WHEN a.allocated_qty > 0 THEN a.allocated_qty * f.price ELSE 0 END)"
            "   / nullif(sum(CASE WHEN a.allocated_qty > 0 THEN a.allocated_qty END), 0)"
            "   AS avg_cost,"
            " min(CASE WHEN a.allocated_qty > 0 THEN f.ts END) AS opened_at"
            " FROM strategy_attribution a"
            " JOIN fills f ON f.id = a.fill_id"
            " JOIN orders o ON o.id = f.order_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE o.account_id = :acct AND f.ts <= :as_of"
            " GROUP BY a.strategy_id, i.symbol HAVING sum(a.allocated_qty) > 0"
            " ORDER BY a.strategy_id, i.symbol"
        ),
        {"acct": account_id, "as_of": as_of},
    ).all()
    return tuple(
        Holding(r.strategy_id, r.symbol, Decimal(r.qty), Decimal(r.avg_cost), r.opened_at)
        for r in rows
        if r.avg_cost is not None and r.opened_at is not None
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
