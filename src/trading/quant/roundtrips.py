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

from trading.core.enums import PositionEffect, Side, effective_effect
from trading.portfolio.adjustments import applied_splits, dividend_allocations, split_factor

_ZERO = Decimal(0)


@dataclass(frozen=True)
class AttributedFill:
    strategy_id: str
    symbol: str
    ts: datetime
    qty: Decimal
    pnl: Decimal
    decision_ts: datetime | None = None
    price: Decimal = _ZERO
    fees: Decimal = _ZERO
    effect: PositionEffect | None = None


@dataclass(frozen=True)
class Roundtrip:
    strategy_id: str
    symbol: str
    entry_ts: datetime
    exit_ts: datetime
    pnl: Decimal
    decision_ts: datetime


@dataclass
class TradeEpisode:
    strategy_id: str
    symbol: str
    entry_ts: datetime
    decision_ts: datetime
    short: bool
    exit_ts: datetime | None = None
    entry_qty: Decimal = _ZERO
    exit_qty: Decimal = _ZERO
    entry_notional: Decimal = _ZERO
    exit_notional: Decimal = _ZERO
    pnl: Decimal = _ZERO
    fees: Decimal = _ZERO


def trade_episodes(fills: Sequence[AttributedFill]) -> list[TradeEpisode]:
    active: dict[tuple[str, str], TradeEpisode] = {}
    closed: list[TradeEpisode] = []
    for fill in fills:
        key = fill.strategy_id, fill.symbol
        episode = active.get(key)
        if fill.qty == 0:
            if episode is not None:
                episode.pnl += fill.pnl
            continue
        short = (
            fill.effect.is_short
            if fill.effect is not None
            else episode.short
            if episode is not None
            else fill.qty < 0
        )
        entry = fill.effect.is_entry if fill.effect is not None else (fill.qty < 0) == short
        if episode is None:
            if not entry:
                continue  # An unattributed entry cannot form a strategy episode.
            episode = active[key] = TradeEpisode(*key, fill.ts, fill.decision_ts or fill.ts, short)
        if episode.short != short:
            continue
        episode.pnl += fill.pnl
        episode.fees += fill.fees
        qty = abs(fill.qty)
        if entry:
            episode.entry_qty += qty
            episode.entry_notional += qty * fill.price
        else:
            episode.exit_qty += qty
            episode.exit_notional += qty * fill.price
            if episode.entry_qty == episode.exit_qty:
                episode.exit_ts = fill.ts
                closed.append(episode)
                del active[key]
    return [*closed, *active.values()]


def _closed(episodes: Sequence[TradeEpisode]) -> list[Roundtrip]:
    return [
        Roundtrip(e.strategy_id, e.symbol, e.entry_ts, e.exit_ts, e.pnl, e.decision_ts)
        for e in episodes
        if e.exit_ts is not None
    ]


def roundtrips(fills: Sequence[AttributedFill]) -> list[Roundtrip]:
    return _closed(trade_episodes(fills))


def load_trade_episodes(session: Session, account_id: UUID, *, end: datetime) -> list[TradeEpisode]:
    rows = session.execute(
        text(
            "SELECT a.strategy_id, i.symbol, f.ts, f.price, a.allocated_qty AS qty, a.pnl,"
            " a.allocated_fees, o.side, o.position_effect, n.created_at AS decision_ts"
            " FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
            " JOIN orders o ON o.id = f.order_id JOIN order_intents n ON n.id = o.intent_id"
            " JOIN instruments i ON i.id = o.instrument_id"
            " LEFT JOIN order_events e ON e.order_id = o.id AND e.reason = 'fill'"
            " AND e.payload_json->>'execution_id' = f.execution_id"
            " WHERE o.account_id = :acct AND f.ts < :end"
            " ORDER BY f.ts, e.seq, f.execution_id, a.strategy_id, a.id"
        ),
        {"acct": account_id, "end": end},
    )
    splits = applied_splits(session, account_id, end)
    fills: list[AttributedFill] = []
    for r in rows:
        factor = split_factor(splits, r.symbol, r.ts)
        # A legacy netted order can contain long-only legs opposite to its venue side.
        side = Side(r.side) if r.position_effect else (Side.BUY if r.qty > 0 else Side.SELL)
        fills.append(
            AttributedFill(
                r.strategy_id,
                r.symbol,
                r.ts,
                r.qty * factor,
                r.pnl or _ZERO,
                r.decision_ts,
                r.price / factor,
                r.allocated_fees,
                effective_effect(side, r.position_effect),
            )
        )
    fills.extend(
        AttributedFill(owner, symbol, ts, _ZERO, pnl)
        for ts, symbol, owner, pnl in dividend_allocations(session, account_id, end)
    )
    return trade_episodes(sorted(fills, key=lambda f: (f.ts, f.qty != 0)))


def load_roundtrips(session: Session, account_id: UUID, *, end: datetime) -> list[Roundtrip]:
    return _closed(load_trade_episodes(session, account_id, end=end))
