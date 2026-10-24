"""Builds the risk engine's account view from the database and records every decision."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.control.state import get_system_state
from trading.core.config import AppConfig
from trading.core.enums import Side
from trading.core.hashing import canonical_json
from trading.core.money import quantize_cash
from trading.data.snapshot import MarketSnapshot
from trading.db.models.market import Instrument
from trading.db.models.portfolio import TradingSession
from trading.db.models.research import Signal
from trading.db.models.trading import Order, RiskDecisionRow
from trading.execution.state_machine import TERMINAL
from trading.portfolio.balances import MissingMark
from trading.portfolio.valuation import PortfolioState
from trading.risk.models import AccountView, ProposedOrder, RiskEngine, RiskEvaluation

# Config entries that are budgets, not order-producing strategies.
_NON_STRATEGIES = frozenset({"cash_reserve"})


@dataclass(frozen=True)
class RecordedDecision:
    decision_id: UUID
    evaluation: RiskEvaluation


def proposal_terms(proposal: ProposedOrder) -> dict[str, object]:
    """The economic terms of a proposal. Persisted with the decision and re-checked by the
    execution manager, so an order can never carry terms the risk engine did not evaluate."""
    return {
        "symbol": proposal.instrument.symbol,
        "side": proposal.side,
        "quantity": proposal.quantity,
        "order_type": proposal.order_type,
        "limit_price": proposal.limit_price,
        "time_in_force": proposal.time_in_force,
        "strategy_id": proposal.strategy_id,
        "strategy_ids": list(proposal.strategies),
    }


def build_account_view(
    session: Session,
    *,
    account_id: UUID,
    portfolio: PortfolioState | None,
    session_date: date,
    config: AppConfig,
    ask_by_symbol: Mapping[str, Decimal] | None = None,
) -> AccountView:
    """Authoritative account state for a decision. Missing prices raise rather than guess."""
    state = get_system_state(session)
    asks = ask_by_symbol or {}
    open_orders = session.execute(
        select(Order, Instrument.symbol, Instrument.sector)
        .join(Instrument, Instrument.id == Order.instrument_id)
        .where(
            Order.account_id == account_id,
            Order.status.not_in([s.value for s in TERMINAL]),
        )
    ).all()
    buy_by_symbol: dict[str, Decimal] = {}
    buy_by_sector: dict[str, Decimal] = {}
    sell_qty: dict[str, Decimal] = {}
    for order, symbol, sector in open_orders:
        remaining = order.qty - order.filled_qty
        if order.side == Side.SELL.value:
            sell_qty[symbol] = sell_qty.get(symbol, Decimal(0)) + remaining
            continue
        price = order.limit_price if order.limit_price is not None else asks.get(symbol)
        if price is None:
            raise MissingMark(f"no reference price for open market buy of {symbol}")
        notional = quantize_cash(remaining * price)
        buy_by_symbol[symbol] = buy_by_symbol.get(symbol, Decimal(0)) + notional
        buy_by_sector[sector] = buy_by_sector.get(sector, Decimal(0)) + notional
    entries = session.scalar(
        select(TradingSession.new_entries).where(
            TradingSession.account_id == account_id, TradingSession.session_date == session_date
        )
    )
    # Every allowed decision's key is consumed, used or not: a retry needs a fresh signal.
    consumed = frozenset(
        session.scalars(
            select(RiskDecisionRow.dedupe_key).where(
                RiskDecisionRow.account_id == account_id, RiskDecisionRow.allowed.is_(True)
            )
        )
    )
    enabled = frozenset(
        name
        for name, strategy in config.strategies.items()
        if strategy.enabled and not strategy.shadow_only and name not in _NON_STRATEGIES
    )
    return AccountView(
        portfolio=portfolio,
        trading_state=state.trading_state,
        risk_state=state.risk_state,
        open_buy_notional_by_symbol=buy_by_symbol,
        open_buy_notional_by_sector=buy_by_sector,
        open_sell_qty_by_symbol=sell_qty,
        new_entries_today=entries or 0,
        consumed_dedupe_keys=consumed,
        enabled_strategies=enabled,
    )


def evaluate_and_record(
    session: Session,
    engine: RiskEngine,
    *,
    proposal: ProposedOrder,
    account: AccountView,
    market: MarketSnapshot,
    portfolio_snapshot_id: UUID | None,
    now: datetime,
) -> RecordedDecision:
    """Run the risk engine and persist the immutable decision, allowed or not."""
    evaluation = engine.evaluate(proposal, account, market, now=now)
    # The FK column is only set when the signal is persisted; the id is always kept in the summary.
    signal_row = session.get(Signal, proposal.signal_id) if proposal.signal_id is not None else None
    summary = {
        **proposal_terms(proposal),
        "attribution": dict(proposal.attribution),
        "signal_id": proposal.signal_id,
        "signal_expires_at": proposal.signal_expires_at,
        "notional": evaluation.notional,
    }
    row = RiskDecisionRow(
        signal_id=signal_row.id if signal_row is not None else None,
        account_id=proposal.account_id,
        instrument_id=proposal.instrument.id,
        ts=now,
        allowed=evaluation.allowed,
        reasons_json=[{"code": r.code.value, "detail": r.detail} for r in evaluation.reasons],
        limits_version=evaluation.limits_version,
        market_snapshot_id=market.id,
        portfolio_snapshot_id=portfolio_snapshot_id,
        dedupe_key=proposal.dedupe_key,
        proposal_json=json.loads(canonical_json(summary)),
    )
    session.add(row)
    session.flush()
    return RecordedDecision(decision_id=row.id, evaluation=evaluation)
