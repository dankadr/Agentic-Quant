from datetime import timedelta
from decimal import Decimal

import pytest

from tests.unit.quant.helpers import OPEN, D, bars_from, make_input, orb_rows
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.quant.models import Holding
from trading.quant.orb import OrbStrategy

PARAMS = {
    "symbols": ["SPY", "QQQ"], "opening_range_minutes": 15, "volume_confirm_ratio": "1.2",
    "breakout_buffer_bps": "1", "target_r": "1.5", "max_hold_minutes": 120, "max_spread_bps": "10",
    "min_avg_volume": "1000", "trade_notional": "80", "signal_ttl_seconds": 60,
}  # fmt: skip
NOW = OPEN + timedelta(minutes=16)  # the breakout bar (minute 15) just closed


def strategy(**over) -> OrbStrategy:  # type: ignore[no-untyped-def]
    return OrbStrategy(strategy_id="orb_v1", version="1.0.0", params={**PARAMS, **over})


def inp(calendar, rows=None, now=NOW, **kw):  # type: ignore[no-untyped-def]
    return make_input(calendar, {"SPY": bars_from(rows or orb_rows())}, now, **kw)


def test_breakout_with_volume_confirmation_buys(calendar: MarketCalendar) -> None:
    (signal,) = strategy().evaluate(inp(calendar))
    assert (signal.strategy_id, signal.strategy_version, signal.symbol, signal.side) == (
        "orb_v1",
        "1.0.0",
        "SPY",
        Side.BUY,
    )
    assert signal.target_notional_usd == D("80")
    assert signal.stop_price == D("99.90")  # the opening-range low
    assert signal.limit_price is not None
    assert D("100.31") < signal.limit_price <= D("100.50")  # above the ask, a bounded ceiling
    assert signal.limit_price % D("0.01") == 0
    assert signal.generated_at == NOW
    assert signal.expires_at - signal.generated_at == timedelta(seconds=60)
    assert signal.reason_codes == ("ORB_BREAK", "VOLUME_CONFIRM")


def test_no_signal_before_the_opening_range_completes(calendar: MarketCalendar) -> None:
    rows = orb_rows()[:10]
    assert strategy().evaluate(inp(calendar, rows, now=OPEN + timedelta(minutes=10))) == []


def test_no_signal_without_a_breakout(calendar: MarketCalendar) -> None:
    assert (
        strategy().evaluate(inp(calendar, orb_rows(breakout_close="100.10"))) == []
    )  # not above range high
    assert (
        strategy().evaluate(inp(calendar, orb_rows(breakout_close="100.105"))) == []
    )  # inside the 1 bp buffer


def test_breakout_needs_volume_confirmation(calendar: MarketCalendar) -> None:
    assert (
        strategy().evaluate(inp(calendar, orb_rows(breakout_volume="1100"))) == []
    )  # < 1.2 x 1000
    assert (
        len(strategy().evaluate(inp(calendar, orb_rows(breakout_volume="1200")))) == 1
    )  # exactly the ratio


def test_illiquid_symbols_are_skipped(calendar: MarketCalendar) -> None:
    assert strategy(min_avg_volume="1001").evaluate(inp(calendar)) == []


def test_wide_spread_or_untradable_data_blocks_entry(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(inp(calendar, quotes={"SPY": ("99.00", "101.00")})) == []
    assert strategy().evaluate(inp(calendar, untradable=frozenset({"SPY"}))) == []


def test_incomplete_opening_range_fails_closed(calendar: MarketCalendar) -> None:
    rows = orb_rows()
    gappy = bars_from(rows[:7]) + bars_from(
        rows[8:], start=OPEN + timedelta(minutes=8)
    )  # minute 7 missing
    assert strategy().evaluate(make_input(calendar, {"SPY": gappy}, NOW)) == []


def test_one_entry_per_symbol_per_session(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(inp(calendar, entered=frozenset({("orb_v1", "SPY")}))) == []
    assert len(strategy().evaluate(inp(calendar, entered=frozenset({("other", "SPY")})))) == 1


def test_bars_that_have_not_closed_are_never_used(calendar: MarketCalendar) -> None:
    """Look-ahead guard inside the strategy, independent of the loader."""
    rows = orb_rows()
    forming = bars_from(rows[:15]) + bars_from(rows[15:], start=OPEN + timedelta(minutes=15))
    early = OPEN + timedelta(minutes=15, seconds=30)  # the breakout bar is still forming
    assert strategy().evaluate(make_input(calendar, {"SPY": forming}, early)) == []


def test_symbols_outside_the_universe_param_are_ignored(calendar: MarketCalendar) -> None:
    data = make_input(calendar, {"AAPL": bars_from(orb_rows(), symbol="AAPL")}, NOW)
    assert strategy().evaluate(data) == []


def test_evaluation_is_deterministic(calendar: MarketCalendar) -> None:
    data = inp(calendar)
    assert strategy().evaluate(data) == strategy().evaluate(data)


# ---- exits --------------------------------------------------------------------------------------


def holding(qty: str = "0.8", cost: str = "100.30", opened_min: int = 16) -> Holding:
    return Holding("orb_v1", "SPY", D(qty), D(cost), OPEN + timedelta(minutes=opened_min))


def exit_input(calendar, close: str, now_min: int = 30, **kw):  # type: ignore[no-untyped-def]
    rows = orb_rows() + [("100.30", "100.40", "100.00", close, "800")] * (now_min - 16)
    return make_input(calendar, {"SPY": bars_from(rows)}, OPEN + timedelta(minutes=now_min), holdings=(holding(**kw),),
                      quotes={"SPY": (str(Decimal(close) - Decimal("0.01")), str(Decimal(close) + Decimal("0.01")))})  # fmt: skip


def test_stop_exit(calendar: MarketCalendar) -> None:
    (signal,) = strategy().evaluate(exit_input(calendar, "99.85"))
    assert (signal.side, signal.reason_codes) == (Side.SELL, ("STOP",))
    assert signal.target_notional_usd == D("0.8") * D("99.85")  # the whole position's value
    assert signal.limit_price is not None
    assert signal.limit_price < D("99.85")  # marketable: below the bid


def test_target_exit(calendar: MarketCalendar) -> None:
    # entry 100.30, stop (range low) 99.90, risk 0.40 -> target 100.30 + 1.5 * 0.40 = 100.90
    (signal,) = strategy().evaluate(exit_input(calendar, "100.90"))
    assert signal.reason_codes == ("TARGET",)


def test_time_stop_exit(calendar: MarketCalendar) -> None:
    rows_now = 16 + 120
    (signal,) = strategy().evaluate(exit_input(calendar, "100.35", now_min=rows_now))
    assert signal.reason_codes == ("TIME_STOP",)


def test_holding_with_nothing_triggered_stays(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(exit_input(calendar, "100.35")) == []


def test_no_new_entry_while_holding(calendar: MarketCalendar) -> None:
    data = make_input(calendar, {"SPY": bars_from(orb_rows())}, NOW, holdings=(holding(),))
    assert strategy().evaluate(data) == []


def test_other_strategies_holdings_do_not_trigger_exits(calendar: MarketCalendar) -> None:
    other = Holding("vwap_v1", "SPY", D("1"), D("100"), OPEN)
    data = make_input(
        calendar, {"SPY": bars_from(orb_rows(breakout_close="99.80"))}, NOW, holdings=(other,)
    )
    assert strategy().evaluate(data) == []


@pytest.mark.parametrize(
    "bad", [{"opening_range_minutes": 0}, {"trade_notional": "0"}, {"signal_ttl_seconds": 600}]
)
def test_invalid_parameters_are_rejected_at_construction(bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        strategy(**bad)
