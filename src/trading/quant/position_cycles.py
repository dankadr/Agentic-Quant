"""Split-adjusted applied fills since each ticker's last flat position."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.portfolio.adjustments import applied_splits, split_factor


@dataclass(frozen=True)
class CycleFill:
    symbol: str
    ts: datetime
    price: Decimal
    qty: Decimal
    attribution: dict[str, Decimal]


def load_current_cycle_fills(
    session: Session, account_id: UUID, as_of: datetime
) -> list[CycleFill]:
    """Use ledger application order, including tied/late timestamps, to find flat boundaries.

    Include unattributed fills in the actual share balance. Normalize all fills to today's
    split-adjusted units before finding boundaries, for long positions and short covers alike.
    Legacy imported fills without events retain the existing timestamp/execution-id fallback.
    """
    rows = session.execute(
        text(
            "SELECT f.id, f.ts, f.execution_id, f.price, f.qty, o.side, i.symbol, e.seq,"
            " coalesce(jsonb_object_agg(a.strategy_id, a.allocated_qty::text)"
            " FILTER (WHERE a.strategy_id IS NOT NULL), '{}'::jsonb) AS attribution"
            " FROM fills f JOIN orders o ON o.id = f.order_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " LEFT JOIN order_events e ON e.order_id = o.id AND e.reason = 'fill'"
            " AND e.payload_json->>'execution_id' = f.execution_id"
            " LEFT JOIN strategy_attribution a ON a.fill_id = f.id"
            " WHERE o.account_id = :acct AND f.ts <= :as_of"
            " AND (e.ts <= :as_of OR e.seq IS NULL)"
            # During attribution the current FillRow exists but is not yet an applied fill.
            " AND (e.seq IS NOT NULL OR a.strategy_id IS NOT NULL)"
            " GROUP BY f.id, o.side, i.symbol, e.seq"
            " ORDER BY e.seq NULLS LAST, f.ts, f.execution_id"
        ),
        {"acct": account_id, "as_of": as_of},
    ).all()
    if any(row.seq is None for row in rows):
        # Imported legacy attribution has no application sequence; retain its prior ordering.
        rows = sorted(rows, key=lambda row: (row.ts, row.seq or 0, row.execution_id))
    splits = applied_splits(session, account_id, as_of)
    balances: dict[str, Decimal] = defaultdict(Decimal)
    cycles: dict[str, list[CycleFill]] = defaultdict(list)
    for row in rows:
        factor = split_factor(splits, row.symbol, row.ts)
        qty = row.qty * factor * (1 if row.side == "BUY" else -1)
        cycles[row.symbol].append(
            CycleFill(
                symbol=row.symbol,
                ts=row.ts,
                price=row.price / factor,
                qty=qty,
                attribution={s: Decimal(str(q)) * factor for s, q in row.attribution.items()},
            )
        )
        balances[row.symbol] += qty
        if balances[row.symbol] == 0:
            cycles[row.symbol].clear()
    return [fill for cycle in cycles.values() for fill in cycle]
