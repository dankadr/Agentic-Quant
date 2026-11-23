"""Durable, account-scoped ticker ownership from positions and unresolved orders."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from trading.execution.state_machine import TERMINAL
from trading.quant.position_cycles import load_current_cycle_fills

UNKNOWN_OWNER = ""  # invalid strategy id: reserve unattributed inventory until reconciled


def load_symbol_owners(
    session: Session, account_id: UUID, as_of: datetime
) -> dict[str, frozenset[str]]:
    positions: dict[str, Decimal] = dict(
        session.execute(
            text(
                "SELECT i.symbol, p.qty FROM positions p JOIN instruments i ON i.id=p.instrument_id"
                " WHERE p.account_id=:acct AND p.qty<>0"
            ),
            {"acct": account_id},
        ).all()
    )
    attributed: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for fill in load_current_cycle_fills(session, account_id, as_of):
        for strategy, qty in fill.attribution.items():
            attributed[fill.symbol, strategy] += qty
    owners: dict[str, set[str]] = defaultdict(set)
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for (symbol, strategy), qty in attributed.items():
        if symbol in positions and qty != 0:
            owners[symbol].add(strategy)
            totals[symbol] += qty
    for symbol, qty in positions.items():
        if totals[symbol] != qty:
            owners[symbol].add(UNKNOWN_OWNER)
    rows = session.execute(
        text(
            "SELECT i.symbol, s.strategy_id FROM orders o"
            " JOIN order_intents n ON n.id=o.intent_id"
            " JOIN instruments i ON i.id=o.instrument_id"
            " LEFT JOIN LATERAL unnest(n.strategy_ids) AS s(strategy_id) ON true"
            " WHERE o.account_id=:acct AND o.status NOT IN :terminal AND o.created_at<=:as_of"
        ).bindparams(bindparam("terminal", expanding=True)),
        {
            "acct": account_id,
            "as_of": as_of,
            "terminal": [s.value for s in TERMINAL],
        },
    )
    for row in rows:
        owners[row.symbol].add(row.strategy_id or UNKNOWN_OWNER)
    return {symbol: frozenset(strategies) for symbol, strategies in owners.items()}
