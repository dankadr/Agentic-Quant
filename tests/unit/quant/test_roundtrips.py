from datetime import timedelta
from decimal import Decimal as D

from tests.factories import T0
from trading.quant.roundtrips import AttributedFill, roundtrips


def fill(minute: int, qty: str, pnl: str) -> AttributedFill:
    return AttributedFill("orb_v1", "SPY", T0 + timedelta(minutes=minute), D(qty), D(pnl))


def test_partial_fills_and_fees_form_one_closed_roundtrip() -> None:
    trips = roundtrips(
        [fill(0, "2", "-0.2"), fill(1, "3", "-0.3"), fill(10, "-2", "4"), fill(20, "-3", "6")]
    )
    assert len(trips) == 1
    assert trips[0].pnl == D("9.5")
    assert trips[0].entry_ts == T0
    assert trips[0].exit_ts == T0 + timedelta(minutes=20)


def test_reentry_is_a_separate_episode_and_open_lot_is_not_a_closed_trade() -> None:
    trips = roundtrips(
        [
            fill(0, "1", "0"),
            fill(10, "-1", "2"),
            fill(40, "1", "-0.1"),
            fill(50, "-1", "-3"),
            fill(80, "1", "-0.1"),
        ]
    )
    assert [t.pnl for t in trips] == [D("2"), D("-3.1")]


def test_short_partial_covers_close_once_with_all_fees() -> None:
    trips = roundtrips([fill(0, "-2", "-0.2"), fill(1, "1", "4.9"), fill(2, "1", "-1.1")])
    assert [t.pnl for t in trips] == [D("3.6")]
