"""Deterministic risk engine. Code-enforced limits: nothing here can be relaxed by a prompt.

Every check runs and every breach is reported, in ``RejectionCode`` declaration order, so the
result is stable and complete. Missing or ambiguous information denies (fail closed).
Exits that reduce risk stay allowed during loss/drawdown halts and the entry cutoff, but
nothing is allowed when the risk state is UNKNOWN or RECONCILIATION_REQUIRED.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from trading.control.state import RiskState, TradingState
from trading.core.codes import RejectionCode
from trading.core.config import PaperBrokerConfig, RiskPolicy
from trading.core.enums import AssetType, OrderType, Side, TimeInForce
from trading.core.fees import estimate_fees
from trading.core.money import is_multiple_of
from trading.data.calendar import CalendarUnavailable, MarketCalendar
from trading.data.instruments import qty_increment
from trading.data.models import BLOCKING_FLAGS, QualityFlag
from trading.data.snapshot import MarketSnapshot
from trading.risk.models import AccountView, ProposedOrder, RiskEvaluation, RiskReason

_ZERO = Decimal(0)
_SUPPORTED_ASSETS = {AssetType.EQUITY, AssetType.ETF}


class DeterministicRiskEngine:
    def __init__(
        self,
        policy: RiskPolicy,
        *,
        calendar: MarketCalendar,
        paper_cfg: PaperBrokerConfig,
        limits_version: str,
    ) -> None:
        self._policy = policy
        self._calendar = calendar
        self._paper = paper_cfg
        self._limits_version = limits_version

    def evaluate(
        self,
        proposal: ProposedOrder,
        account: AccountView,
        market: MarketSnapshot,
        *,
        now: datetime,
    ) -> RiskEvaluation:
        policy = self._policy
        reasons: list[RiskReason] = []

        def deny(code: RejectionCode, detail: str = "") -> None:
            reasons.append(RiskReason(code, detail))

        instrument = proposal.instrument
        symbol = instrument.symbol
        is_entry = proposal.side is Side.BUY
        view = market.symbols.get(symbol)
        quote = view.quote if view is not None else None
        pf = account.portfolio

        ref_price: Decimal | None = None
        if proposal.order_type is OrderType.LIMIT:
            ref_price = proposal.limit_price
        elif quote is not None and quote.ask > 0:
            ref_price = quote.ask
        notional = (
            proposal.quantity * ref_price
            if ref_price is not None and ref_price > 0 and proposal.quantity > 0
            else None
        )

        # 1-5: system and risk state
        if account.trading_state is not TradingState.PAPER_RUNNING:
            deny(RejectionCode.TRADING_PAUSED)
        if account.risk_state is RiskState.UNKNOWN:
            deny(RejectionCode.RISK_STATE_UNKNOWN, "risk_state")
        elif pf is None:
            deny(RejectionCode.RISK_STATE_UNKNOWN, "no_portfolio")
        elif view is None:
            deny(RejectionCode.RISK_STATE_UNKNOWN, "no_market_data")
        if account.risk_state is RiskState.RECONCILIATION_REQUIRED:
            deny(RejectionCode.RECONCILIATION_REQUIRED)
        if is_entry and account.risk_state is RiskState.HALTED_DAILY_LOSS:
            deny(RejectionCode.DAILY_LOSS_LIMIT)
        if is_entry and account.risk_state is RiskState.HALTED_DRAWDOWN:
            deny(RejectionCode.DRAWDOWN_LIMIT)

        # 6: session
        try:
            session = self._calendar.session_at(now)
        except CalendarUnavailable:
            deny(RejectionCode.MARKET_CLOSED, "calendar_unavailable")
        else:
            cutoff = timedelta(minutes=policy.entry_cutoff_minutes_before_close)
            if session is None:
                deny(RejectionCode.MARKET_CLOSED, "closed")
            elif is_entry and session.close_at - now <= cutoff:
                deny(RejectionCode.MARKET_CLOSED, "entry_cutoff")

        # 7-9: signal and data freshness, quote sanity. The snapshot's own stale/tradable verdict
        # is only a hint: freshness is re-derived here from `now`, because this engine is the
        # independent control and must not trust a reused or mislabelled snapshot.
        if proposal.signal_expires_at is not None and now >= proposal.signal_expires_at:
            deny(RejectionCode.SIGNAL_EXPIRED)
        if view is not None:
            max_age = timedelta(seconds=policy.max_bar_age_seconds)
            if view.stale or not view.tradable:
                deny(RejectionCode.STALE_MARKET_DATA, "stale" if view.stale else "blocking_flag")
            if market.as_of > now:
                deny(RejectionCode.STALE_MARKET_DATA, "snapshot_from_future")
            elif now - market.as_of > max_age:
                deny(RejectionCode.STALE_MARKET_DATA, "snapshot_age")
            if quote is not None and quote.ts <= now and now - quote.ts > max_age:
                deny(RejectionCode.STALE_MARKET_DATA, "quote_age")
            if quote is None:
                deny(RejectionCode.INVALID_QUOTE, "no_quote")
            elif quote.ts > now:
                deny(RejectionCode.INVALID_QUOTE, "future")
            elif quote.bid <= 0:
                deny(RejectionCode.INVALID_QUOTE, "non_positive")
            elif quote.ask < quote.bid:
                deny(RejectionCode.INVALID_QUOTE, "crossed")
            elif quote.spread_bps > policy.max_spread_bps:
                deny(RejectionCode.INVALID_QUOTE, "spread")
            if any(QualityFlag(f) in BLOCKING_FLAGS for f in view.quote_flags):
                deny(RejectionCode.INVALID_QUOTE, "quote_flag")

        # 10-11: strategy and duplicates
        # Exits stay allowed for a disabled strategy: its shares must be flattenable.
        if is_entry and any(s not in account.enabled_strategies for s in proposal.strategies):
            deny(RejectionCode.STRATEGY_DISABLED)
        if proposal.dedupe_key in account.consumed_dedupe_keys:
            deny(RejectionCode.DUPLICATE_INTENT)

        # 12-15: order validity
        if (
            proposal.order_type not in (OrderType.LIMIT, OrderType.MARKET)
            or proposal.time_in_force is not TimeInForce.DAY
            or (proposal.order_type is OrderType.LIMIT and proposal.limit_price is None)
            or instrument.asset_type not in _SUPPORTED_ASSETS
        ):
            deny(RejectionCode.UNSUPPORTED_ORDER)
        if not is_entry:
            held = _ZERO
            if pf is not None:
                held = next((p.qty for p in pf.positions if p.symbol == symbol), _ZERO)
            sellable = held - account.open_sell_qty_by_symbol.get(symbol, _ZERO)
            if proposal.quantity > sellable:
                deny(RejectionCode.SHORT_NOT_ALLOWED)
        if proposal.limit_price is not None and (
            proposal.limit_price <= 0
            or not is_multiple_of(proposal.limit_price, instrument.tick_size)
        ):
            deny(RejectionCode.INVALID_TICK_SIZE)
        if proposal.quantity <= 0 or not is_multiple_of(
            proposal.quantity, qty_increment(instrument, self._paper)
        ):
            deny(RejectionCode.INVALID_QUANTITY)

        # 16-20: entry-only limits (they need a portfolio and a price to be evaluated)
        if is_entry and pf is not None and notional is not None:
            equity = pf.equity
            sector = instrument.sector
            symbol_total = (
                pf.exposure_by_symbol.get(symbol, _ZERO)
                + account.open_buy_notional_by_symbol.get(symbol, _ZERO)
                + notional
            )
            if symbol_total > policy.max_symbol_exposure_pct * equity:
                deny(RejectionCode.EXPOSURE_LIMIT, "symbol")
            sector_total = (
                pf.exposure_by_sector.get(sector, _ZERO)
                + account.open_buy_notional_by_sector.get(sector, _ZERO)
                + notional
            )
            if sector_total > policy.max_sector_exposure_pct * equity:
                deny(RejectionCode.EXPOSURE_LIMIT, "sector")
            gross_total = (
                pf.gross_exposure
                + sum(account.open_buy_notional_by_symbol.values(), _ZERO)
                + notional
            )
            if gross_total > policy.max_gross_exposure_pct * equity:
                deny(RejectionCode.EXPOSURE_LIMIT, "gross")

            fees = estimate_fees(Side.BUY, proposal.quantity, ref_price or _ZERO, self._paper)
            if notional + fees > pf.buying_power:
                deny(RejectionCode.INSUFFICIENT_CASH)
            # Settled cash is used: unsettled proceeds do not count toward the reserve.
            cash_after = pf.settled_cash - pf.reserved_cash - notional - fees
            if cash_after < policy.min_cash_reserve_pct * equity:
                deny(RejectionCode.CASH_RESERVE_LIMIT)
            if account.new_entries_today >= policy.max_new_entries_per_day:
                deny(RejectionCode.MAX_DAILY_ENTRIES)
            open_symbols = {p.symbol for p in pf.positions if p.qty > 0} | {
                s for s, n in account.open_buy_notional_by_symbol.items() if n > 0
            }
            if symbol not in open_symbols and len(open_symbols) >= policy.max_open_symbols:
                deny(RejectionCode.MAX_OPEN_SYMBOLS)
        elif is_entry and account.new_entries_today >= policy.max_new_entries_per_day:
            deny(RejectionCode.MAX_DAILY_ENTRIES)

        return RiskEvaluation(
            allowed=not reasons,
            reasons=tuple(reasons),
            limits_version=self._limits_version,
            is_entry=is_entry,
            notional=notional,
        )
