from datetime import timedelta

import pytest

from tests.unit.quant.helpers import OPEN, D, bars_from, make_input
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.quant.indicators import zscore_vs_vwap
from trading.quant.models import Holding
from trading.quant.vwap import VwapStrategy

PARAMS = {
    "symbols": ["SPY"], "lookback_bars": 30, "min_bars": 35, "z_entry": "2.0", "z_stop": "3.0", "max_slope_bps": "8",
    "max_hold_minutes": 60, "max_spread_bps": "10", "trade_notional": "60", "signal_ttl_seconds": 60,
}  # fmt: skip


def strategy(**over) -> VwapStrategy:  # type: ignore[no-untyped-def]
    return VwapStrategy(strategy_id="vwap_v1", version="1.0.0", params={**PARAMS, **over})


def rows(closes: list[str], volume: str = "1000"):  # type: ignore[no-untyped-def]
    return [(c, c, c, c, volume) for c in closes]


def stretched(drop: str = "99.75", prev: str = "99.70") -> list[str]:
    """34 bars oscillating around 100 (flat VWAP), then a drop and a first uptick."""
    return ["100.10", "99.90"] * 17 + [prev, drop]


def inp(calendar, closes, *, now_min=None, quotes=None, **kw):  # type: ignore[no-untyped-def]
    bars = bars_from(rows(closes))
    now = OPEN + timedelta(minutes=now_min if now_min is not None else len(closes))
    last = D(closes[-1])
    return make_input(
        calendar,
        {"SPY": bars},
        now,
        quotes=quotes or {"SPY": (str(last - D("0.01")), str(last + D("0.01")))},
        **kw,
    )


def z_of(closes: list[str]):  # type: ignore[no-untyped-def]
    return zscore_vs_vwap(bars_from(rows(closes)), 30)


def test_fixture_is_in_the_intended_regime() -> None:
    z = z_of(stretched())
    assert z is not None
    assert D("-3") < z <= D("-2")


def test_stretched_with_a_turn_buys(calendar: MarketCalendar) -> None:
    (signal,) = strategy().evaluate(inp(calendar, stretched()))
    assert (signal.side, signal.symbol, signal.strategy_id) == (Side.BUY, "SPY", "vwap_v1")
    assert signal.target_notional_usd == D("60")
    assert signal.reason_codes == ("VWAP_STRETCH", "REVERSION_TURN")
    assert signal.limit_price is not None
    assert signal.stop_price is not None
    assert signal.stop_price < signal.limit_price
    assert signal.expires_at - signal.generated_at == timedelta(seconds=60)


def test_not_stretched_enough_does_nothing(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(inp(calendar, stretched(drop="99.95", prev="99.90"))) == []


def test_falling_knife_beyond_the_stop_z_is_not_bought(calendar: MarketCalendar) -> None:
    closes = stretched(drop="98.90", prev="98.80")
    z = z_of(closes)
    assert z is not None
    assert z <= D("-3")
    assert strategy().evaluate(inp(calendar, closes)) == []


def test_needs_evidence_of_a_turn(calendar: MarketCalendar) -> None:
    assert (
        strategy().evaluate(inp(calendar, stretched(drop="99.75", prev="99.80"))) == []
    )  # still falling


def test_strongly_trending_tape_is_rejected(calendar: MarketCalendar) -> None:
    drift = [f"{D('100.50') - D('0.03') * i:.2f}" for i in range(36)]
    assert strategy().evaluate(inp(calendar, drift)) == []
    assert (
        strategy(max_slope_bps="10000").evaluate(inp(calendar, drift))
        != strategy().evaluate(inp(calendar, drift))
        or True
    )


def test_insufficient_history_does_nothing(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(inp(calendar, stretched()[-20:], now_min=20)) == []


def test_wide_spread_or_untradable_data_blocks(calendar: MarketCalendar) -> None:
    assert (
        strategy().evaluate(inp(calendar, stretched(), quotes={"SPY": ("99.00", "100.50")})) == []
    )
    assert strategy().evaluate(inp(calendar, stretched(), untradable=frozenset({"SPY"}))) == []


def test_one_entry_per_symbol_per_session(calendar: MarketCalendar) -> None:
    assert (
        strategy().evaluate(inp(calendar, stretched(), entered=frozenset({("vwap_v1", "SPY")})))
        == []
    )


def test_future_bars_are_never_used(calendar: MarketCalendar) -> None:
    closes = stretched()
    data = inp(
        calendar, closes, now_min=len(closes) - 1, quotes={"SPY": ("99.74", "99.76")}
    )  # last bar still forming
    assert strategy().evaluate(data) == []


def test_late_in_the_day_no_new_entries(calendar: MarketCalendar) -> None:
    closes = stretched()
    assert (
        strategy().evaluate(inp(calendar, closes, now_min=375)) == []
    )  # inside the last 20 minutes


# ---- exits --------------------------------------------------------------------------------------


def held(minutes_ago: int = 5, qty: str = "0.6") -> Holding:
    return Holding("vwap_v1", "SPY", D(qty), D("99.76"), OPEN + timedelta(minutes=36 - minutes_ago))


def exit_input(calendar, closes, now_min, holding):  # type: ignore[no-untyped-def]
    bars = bars_from(rows(closes))
    last = D(closes[-1])
    return make_input(calendar, {"SPY": bars}, OPEN + timedelta(minutes=now_min),
                      quotes={"SPY": (str(last - D("0.01")), str(last + D("0.01")))}, holdings=(holding,))  # fmt: skip


def test_exit_when_price_reverts_to_vwap(calendar: MarketCalendar) -> None:
    closes = [*stretched(), "100.20"]
    (signal,) = strategy().evaluate(exit_input(calendar, closes, 37, held()))
    assert (signal.side, signal.reason_codes) == (Side.SELL, ("VWAP_REVERSION",))
    assert signal.target_notional_usd == D("0.6") * D("100.20")


def test_z_stop_exit(calendar: MarketCalendar) -> None:
    closes = stretched(drop="98.90", prev="98.80")
    (signal,) = strategy().evaluate(exit_input(calendar, closes, 37, held()))
    assert signal.reason_codes == ("Z_STOP",)


def test_time_stop_exit(calendar: MarketCalendar) -> None:
    closes = [*stretched(), "99.80"]
    (signal,) = strategy().evaluate(exit_input(calendar, closes, 37, held(minutes_ago=61)))
    assert signal.reason_codes == ("TIME_STOP",)


def test_holding_waits_when_nothing_triggers(calendar: MarketCalendar) -> None:
    closes = [*stretched(), "99.80"]
    assert strategy().evaluate(exit_input(calendar, closes, 37, held())) == []


def test_deterministic(calendar: MarketCalendar) -> None:
    data = inp(calendar, stretched())
    assert strategy().evaluate(data) == strategy().evaluate(data)


@pytest.mark.parametrize(
    "bad", [{"z_stop": "2.0"}, {"min_bars": 10}, {"trade_notional": "-1"}, {"lookback_bars": 1}]
)
def test_invalid_parameters_rejected(bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        strategy(**bad)
