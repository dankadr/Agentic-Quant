from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading.data.models import Bar
from trading.quant.indicators import (
    atr,
    rolling_std,
    session_vwap,
    slope_bps,
    vwap_series,
    zscore_vs_vwap,
)

D = Decimal
T0 = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


def bar(i: int, o: str, h: str, l: str, c: str, v: str = "100") -> Bar:  # noqa: E741
    return Bar(symbol="SPY", interval="1m", ts=T0 + timedelta(minutes=i), open=D(o), high=D(h), low=D(l),
               close=D(c), volume=D(v), provider="p", source_seq=None)  # fmt: skip


def test_vwap_uses_typical_price_weighted_by_volume() -> None:
    bars = [bar(0, "10", "12", "8", "10", "100"), bar(1, "10", "16", "10", "13", "300")]
    # typical: (12+8+10)/3 = 10 ; (16+10+13)/3 = 13 -> (10*100 + 13*300) / 400 = 12.25
    assert vwap_series(bars) == [D("10"), D("12.25")]
    assert session_vwap(bars) == D("12.25")


def test_vwap_skips_zero_volume_bars_and_empty_input() -> None:
    bars = [bar(0, "10", "10", "10", "10", "0"), bar(1, "20", "20", "20", "20", "50")]
    assert vwap_series(bars) == [None, D("20")]
    assert session_vwap([]) is None


def test_rolling_std_population() -> None:
    assert rolling_std([D(2), D(4), D(4), D(4), D(5), D(5), D(7), D(9)], 8) == D(2)
    assert rolling_std([D(1), D(2)], 5) is None  # not enough history
    assert rolling_std([D(3), D(3), D(3)], 3) == D(0)


def test_zscore_vs_vwap() -> None:
    # price oscillates around a flat vwap, last close far below
    bars = [
        bar(i, "100", "100", "100", c, "100")
        for i, c in enumerate(["101", "99", "101", "99", "101", "99", "101", "99", "95"])
    ]
    z = zscore_vs_vwap(bars, lookback=9)
    assert z is not None
    assert z < D("-2")
    assert zscore_vs_vwap(bars[:3], lookback=9) is None


def test_zscore_none_when_no_dispersion() -> None:
    flat = [bar(i, "100", "100", "100", "100") for i in range(10)]
    assert zscore_vs_vwap(flat, lookback=10) is None


def test_atr_is_mean_true_range() -> None:
    bars = [
        bar(0, "10", "11", "9", "10"),
        bar(1, "10", "13", "10", "12"),
        bar(2, "12", "12", "9", "10"),
    ]
    # TR: bar1 = max(3, |13-10|, |10-10|) = 3 ; bar2 = max(3, |12-12|, |9-12|) = 3
    assert atr(bars, 2) == D(3)
    assert atr(bars[:1], 2) is None


def test_slope_bps() -> None:
    series = [D("100"), D("100.2"), D("100.5")]
    assert slope_bps(series, 2) == D("50.00")  # (100.5 - 100) / 100 = 0.5% = 50 bp over 2 bars
    assert slope_bps(series, 5) is None
    assert slope_bps([None, D(1), D(2)], 2) is None


@pytest.mark.parametrize("fn", [vwap_series, session_vwap])
def test_no_floats_leak(fn) -> None:  # type: ignore[no-untyped-def]
    out = fn([bar(0, "10", "12", "8", "10")])
    items = out if isinstance(out, list) else [out]
    assert all(isinstance(x, Decimal) for x in items)
