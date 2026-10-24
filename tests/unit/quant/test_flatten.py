from datetime import timedelta

from tests.unit.quant.helpers import D, bars_from, make_input
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.quant.flatten import FlattenStrategy

CLOSE_MINUS_10 = None  # filled from the calendar in each test


def strategy(minutes: int = 10) -> FlattenStrategy:
    return FlattenStrategy(
        strategy_id="eod_flatten", version="1.0.0", params={"flatten_minutes_before_close": minutes}
    )


def data(
    calendar: MarketCalendar, minutes_before_close: float, positions: dict[str, str], quotes=None
):  # type: ignore[no-untyped-def]
    info = calendar.session_on(__import__("datetime").date(2026, 10, 19))
    assert info is not None
    now = info.close_at - timedelta(minutes=minutes_before_close)
    bars = {s: bars_from([("100", "100", "100", "100", "100")], symbol=s) for s in positions}
    return make_input(calendar, bars, now, quotes=quotes if quotes is not None else {s: ("99.99", "100.01") for s in positions},
                      positions={s: D(q) for s, q in positions.items()})  # fmt: skip


def test_nothing_before_the_flatten_window(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(data(calendar, 10.5, {"SPY": "0.5"})) == []


def test_sells_every_held_symbol_from_the_window_start(calendar: MarketCalendar) -> None:
    signals = strategy().evaluate(data(calendar, 10, {"SPY": "0.5", "QQQ": "0.25"}))
    assert sorted(s.symbol for s in signals) == ["QQQ", "SPY"]
    spy = next(s for s in signals if s.symbol == "SPY")
    assert (spy.side, spy.strategy_id, spy.reason_codes) == (
        Side.SELL,
        "eod_flatten",
        ("EOD_FLATTEN",),
    )
    assert spy.target_notional_usd == D("0.5") * D("100.00")  # shares x mid of 99.99 / 100.01
    assert spy.limit_price is not None
    assert spy.limit_price < D("99.99")  # marketable: below the bid
    assert spy.stop_price is None


def test_flat_symbols_and_dust_free_books_need_nothing(calendar: MarketCalendar) -> None:
    assert strategy().evaluate(data(calendar, 5, {"SPY": "0"})) == []
    assert strategy().evaluate(data(calendar, 5, {})) == []


def test_no_signal_without_a_quote_it_cannot_price(calendar: MarketCalendar) -> None:
    d = data(calendar, 5, {"SPY": "0.5"}, quotes={})
    assert strategy().evaluate(d) == []


def test_window_is_configurable(calendar: MarketCalendar) -> None:
    assert len(strategy(30).evaluate(data(calendar, 20, {"SPY": "0.5"}))) == 1
    assert strategy(30).evaluate(data(calendar, 31, {"SPY": "0.5"})) == []


def test_deterministic(calendar: MarketCalendar) -> None:
    d = data(calendar, 5, {"SPY": "0.5"})
    assert strategy().evaluate(d) == strategy().evaluate(d)
