"""Split each fill back to the strategies behind its order.

Analytics only: cash and positions are the source of truth, so a missing strategy row is logged
and skipped, never allowed to block a fill. Per fill: quantity follows the order's recorded signed
weights and always sums to the fill. Each short owner realizes its own basis and allocated fees.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trading.brokers.base import BrokerFill
from trading.core.enums import PositionEffect, Side
from trading.core.money import quantize_cash, quantize_price, trade_cash_amount
from trading.db.models.portfolio import StrategyAttribution
from trading.db.models.research import Strategy
from trading.db.models.trading import FillRow, Order, OrderIntent, RiskDecisionRow
from trading.monitoring.logging import get_logger
from trading.quant.holdings import load_holdings

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
    session: Session,
    *,
    order: Order,
    fill_id: UUID,
    fill: BrokerFill,
    realized: Decimal,
    as_of: datetime,
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
    if order.position_effect and len(weights) > 1 and order.filled_qty:
        # Split cumulative execution, so independently rounded partials telescope to the order.
        cumulative = _split(sign * (order.filled_qty + fill.qty), weights, _QTY_Q)
        previous = dict(
            session.execute(
                select(StrategyAttribution.strategy_id, func.sum(StrategyAttribution.allocated_qty))
                .join(FillRow, FillRow.id == StrategyAttribution.fill_id)
                .where(FillRow.order_id == order.id)
                .group_by(StrategyAttribution.strategy_id)
            ).all()
        )
        qty = {s: cumulative[s] - previous.get(s, Decimal(0)) for s in strategies}
    fees = _split(fill.fees, magnitudes, Decimal("0.0001"))
    # Only legs in the fill direction share realized gains. All legs share fees.
    gross = quantize_cash(realized + fill.fees)
    reducers = {s: m for s, m in magnitudes.items() if weights[s] * sign > 0}
    gross_share = (
        _split(gross, reducers, Decimal("0.0001"))
        if reducers
        else dict.fromkeys(strategies, Decimal(0))
    )
    pnl = {s: gross_share.get(s, Decimal(0)) - fees[s] for s in strategies}

    if order.position_effect in (PositionEffect.OPEN_SHORT, PositionEffect.CLOSE_SHORT):
        holdings = {
            h.strategy_id: h
            for h in load_holdings(session, order.account_id, as_of)
            if h.symbol == fill.symbol
        }
        cash = _split(
            trade_cash_amount(fill.side, fill.qty, fill.price),
            {s: abs(q) for s, q in qty.items()},
            Decimal("0.0001"),
        )
        for owner, delta in qty.items():
            lot = holdings.get(owner)
            before = abs(lot.qty) if lot else Decimal(0)
            cost = lot.avg_cost if lot else Decimal(0)
            after = before - delta
            if after < 0:
                continue  # An externally recorded cover can lack an attributed entry.
            new_cost = cost
            if delta < 0:
                new_cost = quantize_price((before * cost - delta * fill.price) / after)
            pnl[owner] = (
                cash[owner]
                + quantize_cash(before * cost)
                - quantize_cash(after * new_cost)
                - fees[owner]
            )

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
