from decimal import Decimal

import pytest

from trading.quant.performance import daily_sharpe, max_drawdown, trade_stats

D = Decimal


def test_max_drawdown_is_peak_to_trough() -> None:
    assert max_drawdown([D(1000), D(1100), D(1045), D(1200), D(1080)]) == (
        D(120),
        D("0.1"),
    )  # 1200 -> 1080
    assert max_drawdown([D(1000), D(1100)]) == (D(0), D(0))
    assert max_drawdown([]) == (D(0), D(0))
    assert max_drawdown([D(1000)]) == (D(0), D(0))


def test_drawdown_uses_the_deepest_not_the_latest_decline() -> None:
    abs_dd, pct = max_drawdown([D(100), D(80), D(120), D(110)])
    assert (abs_dd, pct) == (D(20), D("0.2"))


def test_trade_stats() -> None:
    stats = trade_stats([D("2"), D("-1"), D("3"), D("-1"), D("1")])
    assert (stats.trades, stats.wins) == (5, 3)
    assert stats.win_rate == D("0.6")
    assert stats.expectancy == D("0.8")  # 4 / 5
    assert stats.avg_win == D(2)
    assert stats.avg_loss == D(-1)
    assert stats.profit_factor == D(3)  # 6 gross win / 2 gross loss


def test_trade_stats_edge_cases() -> None:
    none = trade_stats([])
    assert (none.trades, none.win_rate, none.expectancy, none.profit_factor) == (
        0,
        None,
        None,
        None,
    )
    only_wins = trade_stats([D(1), D(2)])
    assert only_wins.profit_factor is None  # no losses: undefined, never infinity
    assert only_wins.avg_loss is None
    flat = trade_stats([D(0)])
    assert (flat.wins, flat.win_rate) == (0, D(0))  # a scratch trade is not a win


def test_daily_sharpe() -> None:
    assert daily_sharpe([D("0.01"), D("0.01"), D("0.01")]) is None  # no dispersion: undefined
    value = daily_sharpe([D("0.01"), D("-0.01"), D("0.02"), D("0.0")])
    assert value is not None
    assert value > 0
    assert daily_sharpe([D("0.01")]) is None


@pytest.mark.parametrize("fn", [max_drawdown])
def test_no_floats_leak(fn) -> None:  # type: ignore[no-untyped-def]
    abs_dd, pct = fn([D(100), D(90)])
    assert isinstance(abs_dd, Decimal)
    assert isinstance(pct, Decimal)
