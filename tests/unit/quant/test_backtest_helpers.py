from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.quant.backtest import _daily_pnl_returns

D = Decimal
T = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)


def test_daily_returns_are_session_deltas_over_starting_equity() -> None:
    curve = [(T - timedelta(hours=1), D(10)), (T + timedelta(days=1) - timedelta(hours=1), D(5))]
    closes = [T, T + timedelta(days=1)]
    assert _daily_pnl_returns(curve, closes, D(1000)) == (D("0.01"), D("-0.005"))


def test_a_flat_strategy_returns_zero_every_day() -> None:
    assert _daily_pnl_returns([], [T, T + timedelta(days=1)], D(1000)) == (D(0), D(0))


def test_a_non_positive_base_gives_no_returns() -> None:
    assert _daily_pnl_returns([(T, D(1))], [T], D(0)) == ()
