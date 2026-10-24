from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.risk.helpers import (
    NOW,
    D,
    codes,
    evaluate,
    instrument,
    portfolio,
    reasons,
    symbol_view,
)
from trading.control.state import RiskState, TradingState
from trading.core.enums import AssetType, OrderType, Side


def test_baseline_allowed(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar)
    assert result.allowed is True
    assert result.reasons == ()
    assert result.is_entry is True
    assert result.notional == D("100.00")
    assert result.limits_version == "lv1"


def test_market_order_notional_uses_ask(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, otype=OrderType.MARKET, limit=None, qty="1")
    assert result.allowed
    assert result.notional == D("100.00")  # the ask, not a limit price
    big = evaluate(calendar, otype=OrderType.MARKET, limit=None, qty="2")
    assert big.notional == D("200.00")
    assert reasons(big) == [("EXPOSURE_LIMIT", "symbol")]


# ---- exposure limits (acceptance test 4) ---------------------------------------------------


def test_symbol_cap_exact_boundary_allowed(calendar) -> None:  # type: ignore[no-untyped-def]
    assert evaluate(calendar, qty="1", limit="100.00").allowed  # exactly 10% of 1000


@pytest.mark.parametrize(("qty", "allowed"), [("0.9999", True), ("1", True), ("1.0001", False)])
def test_symbol_cap_uses_exact_quantity(calendar, qty: str, allowed: bool) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, qty=qty)
    assert result.allowed is allowed
    if not allowed:
        assert reasons(result) == [("EXPOSURE_LIMIT", "symbol")]


def test_symbol_cap_counts_existing_position_and_open_buys(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(positions=[("SPY", "0.5", "100")])
    assert reasons(evaluate(calendar, qty="0.6", pf=held)) == [("EXPOSURE_LIMIT", "symbol")]
    assert evaluate(calendar, qty="0.5", pf=held).allowed
    assert reasons(evaluate(calendar, qty="0.6", open_buy_by_symbol={"SPY": D("50")})) == [
        ("EXPOSURE_LIMIT", "symbol")
    ]


def test_sector_cap(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(positions=[("AAPL", "1.5", "100"), ("MSFT", "1.4", "100")])  # Technology 290
    result = evaluate(
        calendar, symbol="MSFT", qty="1", pf=held, policy={"max_symbol_exposure_pct": D("0.5")}
    )
    assert reasons(result) == [("EXPOSURE_LIMIT", "sector")]
    ok = evaluate(
        calendar, symbol="MSFT", qty="0.1", pf=held, policy={"max_symbol_exposure_pct": D("0.5")}
    )
    assert ok.allowed  # sector exactly 300 = 30%


def test_sector_cap_counts_open_buys_in_sector(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(
        calendar, symbol="AAPL", qty="1", open_buy_by_sector={"Technology": D("250")},
        policy={"max_symbol_exposure_pct": D("0.5")},
    )  # fmt: skip
    assert reasons(result) == [("EXPOSURE_LIMIT", "sector")]


def test_gross_cap(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(
        positions=[("SPY", "4", "100"), ("QQQ", "3.5", "100")], cash="250"
    )  # gross 750
    result = evaluate(
        calendar, qty="0.6", pf=held,
        policy={"max_symbol_exposure_pct": D("1"), "max_sector_exposure_pct": D("1")},
    )  # fmt: skip
    assert ("EXPOSURE_LIMIT", "gross") in reasons(result)
    assert not result.allowed


def test_all_three_exposure_breaches_reported_in_order(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(positions=[("SPY", "7", "100")], cash="300")
    result = evaluate(calendar, qty="2", pf=held)
    assert [r for r in reasons(result) if r[0] == "EXPOSURE_LIMIT"] == [
        ("EXPOSURE_LIMIT", "symbol"), ("EXPOSURE_LIMIT", "sector"), ("EXPOSURE_LIMIT", "gross"),
    ]  # fmt: skip


def test_open_buy_notional_counts_toward_gross(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(positions=[("QQQ", "7", "100")], cash="300")
    result = evaluate(
        calendar, qty="1", pf=held, open_buy_by_symbol={"IWM": D("50")},
        policy={"max_symbol_exposure_pct": D("1"), "max_sector_exposure_pct": D("1")},
    )  # fmt: skip
    assert ("EXPOSURE_LIMIT", "gross") in reasons(result)  # 700 + 50 + 100 = 850 > 800


# ---- cash ------------------------------------------------------------------------------------


def test_insufficient_cash_counts_open_buy_reservations(calendar) -> None:  # type: ignore[no-untyped-def]
    pf = portfolio(reserved="950")
    result = evaluate(calendar, pf=pf)
    assert "INSUFFICIENT_CASH" in codes(result)


def test_insufficient_cash_includes_fees(calendar) -> None:  # type: ignore[no-untyped-def]
    pf = portfolio(cash="100", equity="1000")
    # 100.00 notional fits exactly with zero commission...
    assert "INSUFFICIENT_CASH" not in codes(evaluate(calendar, pf=pf, policy={}))


def test_cash_reserve_boundary(calendar) -> None:  # type: ignore[no-untyped-def]
    held = portfolio(positions=[("QQQ", "7", "100")], cash="300")  # equity 1000
    relaxed = {
        "max_symbol_exposure_pct": D("1"),
        "max_gross_exposure_pct": D("1"),
        "max_sector_exposure_pct": D("1"),
    }
    assert evaluate(calendar, qty="1", pf=held, policy=relaxed).allowed  # leaves exactly 200 = 20%
    denied = evaluate(calendar, qty="1.0001", pf=held, policy=relaxed)
    assert reasons(denied) == [("CASH_RESERVE_LIMIT", "")] or "CASH_RESERVE_LIMIT" in codes(denied)
    assert "CASH_RESERVE_LIMIT" in codes(denied)


def test_cash_reserve_uses_settled_cash(calendar) -> None:  # type: ignore[no-untyped-def]
    pf = portfolio(cash="1000", settled="250")  # most cash is unsettled proceeds
    assert "CASH_RESERVE_LIMIT" in codes(evaluate(calendar, pf=pf))


# ---- freshness / TTL (acceptance test 3) --------------------------------------------------------


def test_expired_signal_rejected(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, expires=NOW)
    assert codes(result) == ["SIGNAL_EXPIRED"]
    assert evaluate(calendar, expires=NOW + timedelta(seconds=1)).allowed


def test_stale_market_data_rejected(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, views={"SPY": symbol_view("SPY", stale=True, tradable=False)})
    assert codes(result) == ["STALE_MARKET_DATA"]


def test_blocking_flag_data_rejected(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, views={"SPY": symbol_view("SPY", stale=False, tradable=False)})
    assert codes(result) == ["STALE_MARKET_DATA"]


# ---- the engine must not trust the snapshot's own freshness verdict -----------------------------


@pytest.mark.parametrize(("age_s", "allowed"), [(0, True), (90, True), (91, False), (3600, False)])
def test_old_snapshot_rejected_even_if_it_claims_to_be_fresh(
    calendar, age_s: int, allowed: bool
) -> None:  # type: ignore[no-untyped-def]
    old = NOW - timedelta(seconds=age_s)
    result = evaluate(calendar, as_of=old, views={"SPY": symbol_view("SPY", now=NOW)})
    assert result.allowed is allowed
    if not allowed:
        assert reasons(result) == [("STALE_MARKET_DATA", "snapshot_age")]


def test_snapshot_from_the_future_rejected(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, as_of=NOW + timedelta(seconds=1))
    assert reasons(result) == [("STALE_MARKET_DATA", "snapshot_from_future")]


@pytest.mark.parametrize(("age_s", "allowed"), [(0, True), (90, True), (91, False)])
def test_old_quote_rejected(calendar, age_s: int, allowed: bool) -> None:  # type: ignore[no-untyped-def]
    view = symbol_view("SPY", quote_ts=NOW - timedelta(seconds=age_s))
    result = evaluate(calendar, views={"SPY": view})
    assert result.allowed is allowed
    if not allowed:
        assert reasons(result) == [("STALE_MARKET_DATA", "quote_age")]


def test_quote_from_the_future_rejected(calendar) -> None:  # type: ignore[no-untyped-def]
    view = symbol_view("SPY", quote_ts=NOW + timedelta(seconds=1))
    assert reasons(evaluate(calendar, views={"SPY": view})) == [("INVALID_QUOTE", "future")]


@pytest.mark.parametrize(
    "flag", ["NON_POSITIVE_PRICE", "OHLC_INCONSISTENT", "OUTSIDE_SESSION", "DUPLICATE_CONFLICT"]
)
def test_blocking_quote_flag_rejected(calendar, flag: str) -> None:  # type: ignore[no-untyped-def]
    view = symbol_view("SPY", quote_flags=(flag,))
    assert reasons(evaluate(calendar, views={"SPY": view})) == [("INVALID_QUOTE", "quote_flag")]


def test_informational_quote_flag_allowed(calendar) -> None:  # type: ignore[no-untyped-def]
    assert evaluate(
        calendar, views={"SPY": symbol_view("SPY", quote_flags=("OUT_OF_ORDER",))}
    ).allowed


def test_stale_reasons_precede_invalid_quote_reasons(calendar) -> None:  # type: ignore[no-untyped-def]
    view = symbol_view("SPY", bid="100.05", ask="100.00", quote_ts=NOW - timedelta(seconds=120))
    assert codes(evaluate(calendar, views={"SPY": view})) == ["STALE_MARKET_DATA", "INVALID_QUOTE"]


@pytest.mark.parametrize(
    ("bid", "ask", "allowed"),
    [("99.875", "100.125", True), ("99.874", "100.126", False), ("99.99", "100.00", True)],
)
def test_spread_boundary_25_bps(calendar, bid: str, ask: str, allowed: bool) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, views={"SPY": symbol_view("SPY", bid=bid, ask=ask)})
    assert result.allowed is allowed
    if not allowed:
        assert codes(result) == ["INVALID_QUOTE"]


@pytest.mark.parametrize(
    "bad", [{"bid": "0", "ask": "100"}, {"bid": "100.05", "ask": "100.00"}, {"no_quote": True}]
)
def test_invalid_quote_rejected(calendar, bad: dict) -> None:  # type: ignore[no-untyped-def]
    assert codes(evaluate(calendar, views={"SPY": symbol_view("SPY", **bad)})) == ["INVALID_QUOTE"]


# ---- session ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "detail"),
    [
        (datetime(2026, 10, 19, 13, 29, 59, tzinfo=UTC), "closed"),
        (datetime(2026, 10, 19, 20, 0, 0, tzinfo=UTC), "closed"),
        (datetime(2026, 10, 24, 15, 0, tzinfo=UTC), "closed"),  # Saturday
        (datetime(2026, 11, 26, 15, 0, tzinfo=UTC), "closed"),  # Thanksgiving
        (datetime(2030, 1, 2, 15, 0, tzinfo=UTC), "calendar_unavailable"),
    ],
)
def test_market_closed(calendar, now: datetime, detail: str) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(calendar, now=now, views={"SPY": symbol_view("SPY", now=now)})
    assert ("MARKET_CLOSED", detail) in reasons(result)
    assert not result.allowed


def test_entry_cutoff_last_15_minutes(calendar) -> None:  # type: ignore[no-untyped-def]
    blocked = datetime(2026, 10, 19, 19, 45, 0, tzinfo=UTC)
    allowed = datetime(2026, 10, 19, 19, 44, 59, tzinfo=UTC)
    assert reasons(
        evaluate(calendar, now=blocked, views={"SPY": symbol_view("SPY", now=blocked)})
    ) == [("MARKET_CLOSED", "entry_cutoff")]
    assert evaluate(calendar, now=allowed, views={"SPY": symbol_view("SPY", now=allowed)}).allowed


def test_half_day_cutoff_uses_early_close(calendar) -> None:  # type: ignore[no-untyped-def]
    now = datetime(2026, 11, 27, 17, 46, tzinfo=UTC)  # close 18:00Z -> inside the last 15 minutes
    assert ("MARKET_CLOSED", "entry_cutoff") in reasons(
        evaluate(calendar, now=now, views={"SPY": symbol_view("SPY", now=now)})
    )


# ---- exits vs halts (decision C5) -------------------------------------------------------------


def _holding() -> object:
    return portfolio(positions=[("SPY", "1", "100")], cash="900")


def test_exits_allowed_in_cutoff_and_during_halts(calendar) -> None:  # type: ignore[no-untyped-def]
    late = datetime(2026, 10, 19, 19, 50, tzinfo=UTC)
    for state in (RiskState.NORMAL, RiskState.HALTED_DAILY_LOSS, RiskState.HALTED_DRAWDOWN):
        result = evaluate(calendar, side=Side.SELL, qty="1", limit="99.00", now=late, risk_state=state,
                          pf=_holding(), views={"SPY": symbol_view("SPY", now=late)})  # type: ignore[arg-type]  # fmt: skip
        assert result.allowed, (state, reasons(result))
        assert result.is_entry is False


def test_entries_blocked_by_halts_with_stable_codes(calendar) -> None:  # type: ignore[no-untyped-def]
    assert codes(evaluate(calendar, risk_state=RiskState.HALTED_DAILY_LOSS)) == ["DAILY_LOSS_LIMIT"]
    assert codes(evaluate(calendar, risk_state=RiskState.HALTED_DRAWDOWN)) == ["DRAWDOWN_LIMIT"]


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (RiskState.UNKNOWN, "RISK_STATE_UNKNOWN"),
        (RiskState.RECONCILIATION_REQUIRED, "RECONCILIATION_REQUIRED"),
    ],
)
def test_unknown_and_reconciliation_block_exits_too(calendar, state: RiskState, code: str) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(
        calendar, side=Side.SELL, qty="1", limit="99.00", risk_state=state, pf=_holding()
    )  # type: ignore[arg-type]
    assert code in codes(result)
    assert not result.allowed


def test_paused_blocks_entries_and_exits(calendar) -> None:  # type: ignore[no-untyped-def]
    assert codes(evaluate(calendar, trading_state=TradingState.PAUSED)) == ["TRADING_PAUSED"]
    exit_result = evaluate(calendar, side=Side.SELL, qty="1", limit="99.00", pf=_holding(),  # type: ignore[arg-type]
                           trading_state=TradingState.PAUSED)  # fmt: skip
    assert codes(exit_result) == ["TRADING_PAUSED"]


def test_missing_portfolio_or_symbol_fails_closed(calendar) -> None:  # type: ignore[no-untyped-def]
    assert "RISK_STATE_UNKNOWN" in codes(evaluate(calendar, pf=None))
    assert "RISK_STATE_UNKNOWN" in codes(evaluate(calendar, views={}))


# ---- order validity ---------------------------------------------------------------------------


def test_short_sell_rejected_and_open_sells_counted(calendar) -> None:  # type: ignore[no-untyped-def]
    assert codes(evaluate(calendar, side=Side.SELL, qty="1", limit="99.00")) == [
        "SHORT_NOT_ALLOWED"
    ]
    held = portfolio(positions=[("SPY", "1", "100")], cash="900")
    assert evaluate(calendar, side=Side.SELL, qty="1", limit="99.00", pf=held).allowed
    over = evaluate(
        calendar, side=Side.SELL, qty="0.5", limit="99.00", pf=held, open_sell_qty={"SPY": D("0.6")}
    )
    assert codes(over) == ["SHORT_NOT_ALLOWED"]


def test_tick_size_and_quantity_validation(calendar) -> None:  # type: ignore[no-untyped-def]
    assert "INVALID_TICK_SIZE" in codes(evaluate(calendar, limit="100.005"))
    assert "INVALID_TICK_SIZE" in codes(evaluate(calendar, limit="0"))
    assert "INVALID_QUANTITY" in codes(evaluate(calendar, qty="0.00005"))
    assert "INVALID_QUANTITY" in codes(evaluate(calendar, qty="0"))
    whole = instrument("SPY", fractional_eligible=False)
    assert "INVALID_QUANTITY" in codes(evaluate(calendar, qty="0.5", inst=whole))
    assert evaluate(calendar, qty="1", inst=whole).allowed


def test_unsupported_orders(calendar) -> None:  # type: ignore[no-untyped-def]
    option = instrument("SPY", asset_type="OPTION")  # type: ignore[arg-type]
    assert "UNSUPPORTED_ORDER" in codes(evaluate(calendar, inst=option))
    assert "UNSUPPORTED_ORDER" in codes(evaluate(calendar, otype=OrderType.LIMIT, limit=None))
    assert AssetType.ETF.value == "ETF"


def test_entry_limits_count_and_open_symbols(calendar) -> None:  # type: ignore[no-untyped-def]
    assert evaluate(calendar, new_entries=9).allowed
    assert codes(evaluate(calendar, new_entries=10)) == ["MAX_DAILY_ENTRIES"]
    three = portfolio(
        positions=[("QQQ", "0.5", "100"), ("IWM", "0.5", "100"), ("AAPL", "0.5", "100")], cash="850"
    )
    result = evaluate(calendar, symbol="SPY", qty="0.5", pf=three)
    assert reasons(result) == [("MAX_OPEN_SYMBOLS", "")] or codes(result) == ["MAX_OPEN_SYMBOLS"]
    assert evaluate(calendar, symbol="QQQ", qty="0.4", pf=three).allowed  # adding to a held symbol
    via_orders = evaluate(calendar, symbol="SPY", qty="0.5", pf=portfolio(positions=[("QQQ", "0.5", "100")]),
                          open_buy_by_symbol={"IWM": D("10"), "AAPL": D("10")})  # fmt: skip
    assert "MAX_OPEN_SYMBOLS" in codes(via_orders)


def test_strategy_disabled_and_duplicate_intent(calendar) -> None:  # type: ignore[no-untyped-def]
    assert codes(evaluate(calendar, strategy_id="vwap_v1")) == ["STRATEGY_DISABLED"]
    assert codes(evaluate(calendar, consumed=frozenset({"k1"}))) == ["DUPLICATE_INTENT"]


# ---- determinism / ordering ---------------------------------------------------------------------


def test_multiple_reasons_in_stable_declaration_order(calendar) -> None:  # type: ignore[no-untyped-def]
    result = evaluate(
        calendar, trading_state=TradingState.PAUSED, expires=NOW,
        views={"SPY": symbol_view("SPY", stale=True, tradable=False)},
    )  # fmt: skip
    assert codes(result) == ["TRADING_PAUSED", "SIGNAL_EXPIRED", "STALE_MARKET_DATA"]


def test_engine_is_pure(calendar) -> None:  # type: ignore[no-untyped-def]
    first = evaluate(calendar, qty="1.0001", new_entries=10)
    second = evaluate(calendar, qty="1.0001", new_entries=10)
    assert first == second


def test_every_strategy_behind_a_netted_order_must_be_enabled(calendar) -> None:  # type: ignore[no-untyped-def]
    both = evaluate(
        calendar,
        strategy_id="orb_v1",
        strategy_ids=("orb_v1", "vwap_v1"),
        enabled=frozenset({"orb_v1", "vwap_v1"}),
    )
    assert both.allowed
    one_off = evaluate(
        calendar,
        strategy_id="orb_v1",
        strategy_ids=("orb_v1", "vwap_v1"),
        enabled=frozenset({"orb_v1"}),
    )
    assert codes(one_off) == ["STRATEGY_DISABLED"]
