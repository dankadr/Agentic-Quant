"""What each strategy holds and has done, from recorded fills and orders (never from memory)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from trading.core.enums import PositionEffect, Side, effective_effect
from trading.core.money import quantize_price
from trading.execution.state_machine import TERMINAL
from trading.portfolio.adjustments import applied_splits, split_factor
from trading.portfolio.balances import MissingMark
from trading.quant.models import EntryHistory, Holding
from trading.quant.position_cycles import load_current_cycle_fills

_TERMINAL = tuple(s.value for s in TERMINAL)


@dataclass
class _Lot:
    opened_at: datetime
    qty: Decimal = Decimal(0)
    cost: Decimal = Decimal(0)


def load_holdings(session: Session, account_id: UUID, as_of: datetime) -> tuple[Holding, ...]:
    """Open lots per (strategy, symbol), replayed from fill attribution up to ``as_of``.

    A lot starts at the first buy after the strategy was last flat, so yesterday's closed round
    trips never age or re-price today's entry.
    """
    fills = load_current_cycle_fills(session, account_id, as_of)
    lots: dict[tuple[str, str], _Lot] = {}
    for fill in fills:
        for strategy, qty in fill.attribution.items():
            key = (strategy, fill.symbol)
            lot = lots.setdefault(key, _Lot(fill.ts))
            if lot.qty == 0:
                lot = lots[key] = _Lot(fill.ts)
            if lot.qty * qty >= 0 and qty:
                lot.cost = quantize_price(
                    (abs(lot.qty) * lot.cost + abs(qty) * fill.price) / abs(lot.qty + qty)
                )
            lot.qty += qty
    return tuple(
        Holding(strategy, symbol, lot.qty, lot.cost, lot.opened_at)
        for (strategy, symbol), lot in sorted(lots.items())
        if lot.qty != 0
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
            " WHERE o.account_id = :acct AND n.is_entry AND n.created_at >= :since"
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


def load_open_buy_by_strategy(
    session: Session, account_id: UUID, *, asks: Mapping[str, Decimal] | None = None
) -> dict[str, Decimal]:
    """Unfilled entry notional reserved against each contributing strategy's sleeve."""
    rows = session.execute(
        text(
            "SELECT o.side, o.position_effect, o.limit_price, o.qty - o.filled_qty AS remaining,"
            " i.symbol, n.strategy_ids, d.proposal_json->'attribution' AS weights"
            " FROM orders o JOIN order_intents n ON n.id = o.intent_id"
            " JOIN risk_decisions d ON d.id = n.risk_decision_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " WHERE o.account_id = :acct AND o.status NOT IN :terminal"
        ).bindparams(bindparam("terminal", expanding=True)),
        {"acct": account_id, "terminal": list(_TERMINAL)},
    )
    totals: dict[str, Decimal] = {}
    for row in rows:
        effect = effective_effect(
            Side(row.side), PositionEffect(row.position_effect) if row.position_effect else None
        )
        if not effect.is_entry:
            continue
        ask = (asks or {}).get(row.symbol)
        price = row.limit_price or ask
        if price is None or (effect.is_short and ask is None):
            raise MissingMark(f"missing reservation quote for {row.symbol}")
        if effect.is_short and ask is not None:
            price = max(price, ask)
        owners = row.strategy_ids
        weights = {owner: abs(Decimal(str((row.weights or {}).get(owner, 0)))) for owner in owners}
        if not all(weights.values()):
            weights = dict.fromkeys(owners, Decimal(1))
        denominator = sum(weights.values(), Decimal(0))
        for owner, weight in weights.items():
            totals[owner] = (
                totals.get(owner, Decimal(0)) + row.remaining * price * weight / denominator
            )
    return totals


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
    splits = applied_splits(session, account_id, as_of)
    for row in rows:
        key = (row.strategy_id, row.symbol)
        before = qtys.get(key, Decimal(0))
        qty = Decimal(row.qty) * split_factor(splits, row.symbol, row.ts)
        after = before + qty
        qtys[key] = after
        h = histories.get(key, EntryHistory())
        if before == 0 and after != 0:
            if row.ts >= since:
                h = EntryHistory(h.entries + 1, None)
        elif before != 0 and after == 0 and row.ts >= since:
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
            " WHERE o.account_id=:acct AND n.is_entry AND o.created_at <= :at"
            " AND o.status NOT IN :terminal"
        ).bindparams(bindparam("terminal", expanding=True)),
        {"acct": account_id, "at": as_of, "terminal": list(_TERMINAL)},
    )
    for row in pending:
        key = (row.strategy_id, row.symbol)
        h = histories.get(key, EntryHistory())
        histories[key] = EntryHistory(h.entries, h.last_exit_at, True)
    return histories
