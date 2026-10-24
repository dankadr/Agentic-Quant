from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import (
    NEXT_DAY,
    broker_fill,
    get_instrument,
    new_order,
    open_account,
)
from trading.core.config import load_config
from trading.db.models.market import MarketBar
from trading.db.models.portfolio import EquitySnapshot
from trading.portfolio.ledger import apply_fill
from trading.quant.performance import load_report
from trading.quant.registry import seed_strategies

D = Decimal
CFG = load_config(Path("configs"))
START, END = T0 - timedelta(hours=1), T0 + timedelta(hours=8)


def equity(db: Session, account_id, ts: datetime, value: str, kind: str = "INTRADAY") -> None:  # type: ignore[no-untyped-def]
    db.add(EquitySnapshot(account_id=account_id, ts=ts, kind=kind, cash=D(value), settled_cash=D(value), equity=D(value),
                          exposure=D(0), realized_pnl=D(0), unrealized_pnl=D(0), positions_json={}))  # fmt: skip
    db.flush()


def trade(
    db: Session,
    account_id,
    coid: str,
    side: str,
    qty: str,
    price: str,
    at: datetime,
    strategy: str,
    fees: str = "0",
    slip: str = "0",
):  # type: ignore[no-untyped-def]
    order = new_order(
        db,
        account_id,
        coid,
        side=side,
        qty=qty,
        limit="1000" if side == "BUY" else "1",
        strategy_ids=[strategy],
    )
    fill = replace(
        broker_fill(order, f"e-{coid}", qty=qty, price=price, fees=fees),
        slippage_estimate=D(slip),
        ts=at,
    )
    apply_fill(db, account_id=account_id, fill=fill, settles_on=NEXT_DAY, now=at)


def world(db: Session):  # type: ignore[no-untyped-def]
    seed_strategies(db, CFG, now=T0)
    account_id = open_account(db)
    trade(db, account_id, "b1", "BUY", "1", "100", T0, "orb_v1")
    trade(
        db,
        account_id,
        "s1",
        "SELL",
        "1",
        "103",
        T0 + timedelta(hours=1),
        "orb_v1",
        fees="0.02",
        slip="0.01",
    )  # +2.98
    trade(db, account_id, "b2", "BUY", "1", "100", T0 + timedelta(hours=2), "vwap_v1")
    trade(
        db, account_id, "s2", "SELL", "1", "99", T0 + timedelta(hours=3), "vwap_v1", fees="0.01"
    )  # -1.01
    equity(db, account_id, T0, "1000", "SESSION_START")
    equity(db, account_id, T0 + timedelta(hours=1), "1004")
    equity(db, account_id, T0 + timedelta(hours=2), "1010")
    equity(db, account_id, T0 + timedelta(hours=3), "1001.96")
    equity(db, account_id, T0 + timedelta(hours=4), "1002", "SESSION_END")
    return account_id


def test_account_report(db: Session) -> None:
    account_id = world(db)
    r = load_report(db, account_id=account_id, start=START, end=END)
    assert (r.scope, r.curve_kind) == ("account", "equity")
    assert [v for _, v in r.equity_curve] == [D("1004"), D("1010"), D("1001.96"), D("1002")]
    assert (r.start_equity, r.end_equity, r.net_pnl) == (D("1000"), D("1002"), D("2"))
    assert r.return_pct == D("0.002")
    assert (r.max_drawdown_abs, r.max_drawdown_pct.quantize(D("0.00001"))) == (
        D("8.04"),
        D("0.00796"),
    )  # 1010 -> 1001.96
    assert (r.trades.trades, r.trades.wins) == (2, 1)
    assert r.trades.win_rate == D("0.5")
    assert r.costs.fees == D("0.0300")
    assert r.costs.slippage == D("0.0100")
    assert r.costs.total == D("0.0400")
    assert r.fill_ratio == D(1)


def test_trade_pnls_are_net_of_fees(db: Session) -> None:
    account_id = world(db)
    r = load_report(db, account_id=account_id, start=START, end=END)
    assert r.trades.expectancy == D("0.985")  # (2.98 + -1.01) / 2: sells carry their fees
    assert r.trades.profit_factor == D("2.98") / D("1.01")


def test_strategy_report_is_scoped_to_one_strategy(db: Session) -> None:
    account_id = world(db)
    orb = load_report(db, account_id=account_id, start=START, end=END, strategy_id="orb_v1")
    vwap = load_report(db, account_id=account_id, start=START, end=END, strategy_id="vwap_v1")
    assert (orb.scope, orb.curve_kind) == ("orb_v1", "cumulative_pnl")
    assert orb.net_pnl == D("2.98")
    assert vwap.net_pnl == D("-1.01")
    assert (orb.trades.trades, vwap.trades.trades) == (1, 1)
    assert orb.costs.fees == D("0.02")
    assert vwap.costs.fees == D("0.01")
    assert vwap.max_drawdown_abs == D("1.01")  # from the 0 starting point to -1.01
    assert orb.start_equity is None


def test_date_range_limits_the_report(db: Session) -> None:
    account_id = world(db)
    early = load_report(
        db, account_id=account_id, start=START, end=T0 + timedelta(hours=1, minutes=30)
    )
    assert early.trades.trades == 1  # only the first exit is inside the window
    assert early.costs.fees == D("0.0200")
    empty = load_report(db, account_id=account_id, start=END, end=END + timedelta(days=1))
    assert (empty.net_pnl, empty.trades.trades, empty.fill_ratio, empty.start_equity) == (
        D(0),
        0,
        None,
        None,
    )
    assert empty.equity_curve == []


def test_benchmark_buy_and_hold_and_excess_return(db: Session) -> None:
    account_id = world(db)
    spy = get_instrument(db, "SPY")
    for minute, (o, c) in enumerate([("500.00", "501.00"), ("501.00", "505.00")]):
        db.add(MarketBar(instrument_id=spy.id, interval="1m", ts=T0 + timedelta(minutes=minute), provider="replay", open=D(o), high=D(c),
                         low=D(o), close=D(c), volume=D(100), ingested_at=T0, session_date=T0.date(), source_seq=None, quality_flags=[]))  # fmt: skip
    r = load_report(db, account_id=account_id, start=START, end=END)
    assert r.benchmark is not None
    assert (r.benchmark.start_price, r.benchmark.end_price) == (D("500.00"), D("505.00"))
    assert r.benchmark.return_pct == D("0.01")
    assert r.excess_return_pct == D("0.002") - D("0.01")


def test_benchmark_ignores_corrupt_bars_and_is_optional(db: Session) -> None:
    account_id = world(db)
    assert load_report(db, account_id=account_id, start=START, end=END).benchmark is None
    spy = get_instrument(db, "SPY")
    db.add(MarketBar(instrument_id=spy.id, interval="1m", ts=T0, provider="replay", open=D(0), high=D(1), low=D(0), close=D(1), volume=D(1),
                     ingested_at=T0, session_date=T0.date(), source_seq=None, quality_flags=["NON_POSITIVE_PRICE"]))  # fmt: skip
    assert load_report(db, account_id=account_id, start=START, end=END).benchmark is None
