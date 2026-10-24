"""Split each fill back to the strategies behind its order.

Analytics only: cash and positions are the source of truth, so a missing strategy row is logged
and skipped, never allowed to block a fill. Per fill: quantity follows the order's recorded signed
weights (and always sums exactly to the fill); fees and realized P&L follow weight magnitudes.
"""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.brokers.base import BrokerFill
from trading.core.enums import Side
from trading.core.money import quantize_cash
from trading.db.models.portfolio import StrategyAttribution
from trading.db.models.research import Strategy
from trading.db.models.trading import Order, OrderIntent, RiskDecisionRow
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
_QTY_Q = Decimal("0.00000001")


def _split(total: Decimal, weights: dict[str, Decimal], quantum: Decimal) -> dict[str, Decimal]:
    """Split ``total`` proportionally to ``weights`` so the parts sum EXACTLY to ``total``."""
    denominator = sum(weights.values(), Decimal(0))
    if denominator == 0:
        raise ValueError("weights sum to zero")
    parts = {k: (total * w / denominator).quantize(quantum) for k, w in weights.items()}
    remainder = total - sum(parts.values(), Decimal(0))
    if remainder != 0:
        anchor = max(weights, key=lambda k: (abs(weights[k]), k))
        parts[anchor] += remainder
    return parts


def record_attribution(
    session: Session, *, order: Order, fill_id: UUID, fill: BrokerFill, realized: Decimal
) -> None:
    intent = session.get(OrderIntent, order.intent_id)
    if intent is None:
        return
    strategies = sorted(intent.strategy_ids)
    decision = session.get(RiskDecisionRow, intent.risk_decision_id)
    raw = (decision.proposal_json if decision is not None else {}).get("attribution") or {}
    sign = Decimal(1) if fill.side is Side.BUY else Decimal(-1)
    weights = {s: Decimal(str(raw[s])) for s in strategies if s in raw}
    if len(weights) != len(strategies) or sum(weights.values(), Decimal(0)) == 0:
        weights = dict.fromkeys(strategies, sign)  # no recorded weights: equal split
    magnitudes = {s: abs(w) for s, w in weights.items()}

    qty = _split(sign * fill.qty, weights, _QTY_Q)
    fees = _split(fill.fees, magnitudes, Decimal("0.0001"))
    # Realized gross P&L belongs to the strategies that reduced the position; a buying leg on a net
    # SELL is an internal transfer. Every strategy still bears its share of the fees.
    gross = quantize_cash(realized + fill.fees)
    reducers = {s: m for s, m in magnitudes.items() if weights[s] < 0}
    gross_share = (
        _split(gross, reducers, Decimal("0.0001"))
        if reducers
        else dict.fromkeys(strategies, Decimal(0))
    )
    pnl = {s: gross_share.get(s, Decimal(0)) - fees[s] for s in strategies}

    known = set(session.scalars(select(Strategy.id).where(Strategy.id.in_(strategies))))
    for strategy in strategies:
        if strategy not in known:
            _log.error("attribution_skipped_unknown_strategy", strategy=strategy)
            continue
        session.add(
            StrategyAttribution(
                strategy_id=strategy,
                fill_id=fill_id,
                allocated_qty=qty[strategy],
                allocated_fees=fees[strategy],
                pnl=pnl[strategy],
            )
        )
    session.flush()
