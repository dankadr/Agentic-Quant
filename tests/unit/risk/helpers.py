"""Builders for risk-engine unit tests (no database)."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from trading.control.state import RiskState, TradingState
from trading.core.config import load_config
from trading.core.enums import AssetType, OrderType, Side, TimeInForce
from trading.data.calendar import MarketCalendar
from trading.data.instruments import InstrumentInfo
from trading.data.models import Bar, Quote
from trading.data.snapshot import MarketSnapshot, SymbolView
from trading.portfolio.valuation import PortfolioState, PositionView
from trading.risk.engine import DeterministicRiskEngine
from trading.risk.models import AccountView, ProposedOrder, RiskEvaluation

D = Decimal
CONFIG = load_config(Path("configs"))
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
ACCOUNT_ID = uuid4()

SECTORS = {"SPY": "Broad Market", "QQQ": "Broad Market", "IWM": "Broad Market",
           "AAPL": "Technology", "MSFT": "Technology"}  # fmt: skip


def instrument(symbol: str = "SPY", **kw: Any) -> InstrumentInfo:
    base = InstrumentInfo(id=uuid4(), symbol=symbol, asset_type=AssetType.ETF, tick_size=D("0.01"),
                          lot_size=D(1), fractional_eligible=True, sector=SECTORS.get(symbol, "Other"))  # fmt: skip
    return replace(base, **kw)


def symbol_view(symbol: str, *, bid: str = "99.99", ask: str = "100.00", stale: bool = False,
                tradable: bool = True, no_quote: bool = False, now: datetime = NOW,
                quote_ts: datetime | None = None, quote_flags: tuple[str, ...] = ()) -> SymbolView:  # fmt: skip
    quote = None if no_quote else Quote(symbol=symbol, ts=quote_ts or now, bid=D(bid), ask=D(ask),
                                        bid_size=None, ask_size=None, provider="p")  # fmt: skip
    bar = Bar(symbol=symbol, interval="1m", ts=now - timedelta(minutes=1), open=D(100), high=D(100),
              low=D(100), close=D(100), volume=D(1000), provider="p", source_seq=None)  # fmt: skip
    return SymbolView(symbol=symbol, instrument_id=uuid4(), last_bar=bar, quote=quote,
                      bar_age_seconds=0, stale=stale, tradable=tradable, quote_flags=quote_flags)  # fmt: skip


def portfolio(
    *,
    equity: str = "1000",
    positions: list[tuple[str, str, str]] | None = None,  # (symbol, qty, mark)
    cash: str | None = None,
    settled: str | None = None,
    reserved: str = "0",
    realized: str = "0",
) -> PortfolioState:
    views = []
    for symbol, qty, mark in positions or []:
        mv = D(qty) * D(mark)
        views.append(PositionView(symbol=symbol, instrument_id=uuid4(), sector=SECTORS.get(symbol, "Other"),
                                  qty=D(qty), average_cost=D(mark), mark=D(mark), market_value=mv,
                                  unrealized_pnl=D(0)))  # fmt: skip
    gross = sum((v.market_value for v in views), D(0))
    cash_total = D(cash) if cash is not None else D(equity) - gross
    settled_cash = D(settled) if settled is not None else cash_total
    by_sector: dict[str, Decimal] = {}
    for v in views:
        by_sector[v.sector] = by_sector.get(v.sector, D(0)) + v.market_value
    return PortfolioState(
        account_id=ACCOUNT_ID, as_of=NOW, cash=cash_total, settled_cash=settled_cash,
        reserved_cash=D(reserved), buying_power=settled_cash - D(reserved), equity=D(equity),
        gross_exposure=gross, exposure_by_symbol={v.symbol: v.market_value for v in views},
        exposure_by_sector=by_sector, realized_pnl=D(realized), unrealized_pnl=D(0), positions=tuple(views),
    )  # fmt: skip


def evaluate(
    calendar: MarketCalendar,
    *,
    symbol: str = "SPY",
    side: Side = Side.BUY,
    qty: str = "1",
    limit: str | None = "100.00",
    otype: OrderType = OrderType.LIMIT,
    now: datetime = NOW,
    as_of: datetime | None = None,
    trading_state: TradingState = TradingState.PAPER_RUNNING,
    risk_state: RiskState = RiskState.NORMAL,
    pf: PortfolioState | str | None = "default",
    views: dict[str, SymbolView] | None = None,
    expires: datetime | None = None,
    strategy_id: str = "orb_v1",
    dedupe_key: str = "k1",
    new_entries: int = 0,
    consumed: frozenset[str] = frozenset(),
    enabled: frozenset[str] = frozenset({"orb_v1"}),
    open_buy_by_symbol: dict[str, Decimal] | None = None,
    open_buy_by_sector: dict[str, Decimal] | None = None,
    open_sell_qty: dict[str, Decimal] | None = None,
    inst: InstrumentInfo | None = None,
    policy: dict[str, Any] | None = None,
) -> RiskEvaluation:
    instr = inst or instrument(symbol)
    state = portfolio() if pf == "default" else pf
    assert state is None or isinstance(state, PortfolioState)
    market = MarketSnapshot(
        id=uuid4(), as_of=as_of or now, provider="p",
        symbols=views if views is not None else {symbol: symbol_view(symbol, now=now)},
        quality_status="OK", data_hash="h",
    )  # fmt: skip
    proposal = ProposedOrder(
        account_id=ACCOUNT_ID, instrument=instr, side=side, quantity=D(qty), order_type=otype,
        limit_price=D(limit) if limit else None, time_in_force=TimeInForce.DAY, strategy_id=strategy_id,
        signal_id=None, signal_expires_at=expires, dedupe_key=dedupe_key,
    )  # fmt: skip
    account = AccountView(
        portfolio=state, trading_state=trading_state, risk_state=risk_state,
        open_buy_notional_by_symbol=open_buy_by_symbol or {},
        open_buy_notional_by_sector=open_buy_by_sector or {},
        open_sell_qty_by_symbol=open_sell_qty or {}, new_entries_today=new_entries,
        consumed_dedupe_keys=consumed, enabled_strategies=enabled,
    )  # fmt: skip
    risk = CONFIG.risk.model_copy(update=policy or {})
    engine = DeterministicRiskEngine(risk, calendar=calendar, paper_cfg=CONFIG.paper_broker,
                                     limits_version="lv1")  # fmt: skip
    return engine.evaluate(proposal, account, market, now=now)


def codes(result: RiskEvaluation) -> list[str]:
    return [r.code.value for r in result.reasons]


def reasons(result: RiskEvaluation) -> list[tuple[str, str]]:
    return [(r.code.value, r.detail) for r in result.reasons]


__all__ = ["NOW", "UUID", "D", "codes", "evaluate", "instrument", "portfolio", "reasons", "symbol_view"]  # fmt: skip
