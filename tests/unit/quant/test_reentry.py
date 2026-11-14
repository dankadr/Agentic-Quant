from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.quant.helpers import OPEN, bars_from, make_input, orb_rows
from tests.unit.quant.test_orb import strategy as orb
from tests.unit.quant.test_vwap import inp, stretched
from tests.unit.quant.test_vwap import strategy as vwap
from trading.data.calendar import MarketCalendar
from trading.quant import models


@pytest.mark.parametrize("factory", [orb, vwap])
def test_reentry_parameters_preserve_single_entry_default(factory) -> None:
    assert factory().p.max_entries_per_symbol_per_day == 1
    assert factory().p.reentry_cooldown_minutes == 30


@pytest.mark.parametrize("count,elapsed,want", [(1, 29, 0), (1, 30, 1), (2, 60, 0)])
@pytest.mark.parametrize("kind", ["orb", "vwap"])
def test_reentry_limit_and_cooldown(
    calendar: MarketCalendar, kind: str, count: int, elapsed: int, want: int
) -> None:
    now = OPEN + timedelta(minutes=90)
    if kind == "orb":
        data = make_input(calendar, {"SPY": bars_from(orb_rows())}, now)
        strategy = orb(max_entries_per_symbol_per_day=2)
    else:
        data = inp(calendar, stretched(), now_min=90)
        strategy = vwap(max_entries_per_symbol_per_day=2)
    history = models.EntryHistory(entries=count, last_exit_at=now - timedelta(minutes=elapsed))
    data = replace(
        data,
        entered_today=frozenset({(strategy.strategy_id, "SPY")}),
        entry_history={(strategy.strategy_id, "SPY"): history},
    )
    assert len(strategy.evaluate(data)) == want
    if not want:
        assert strategy.entry_diagnostics["SPY"] == (
            "ENTRY_LIMIT" if count == 2 else "REENTRY_COOLDOWN"
        )


@pytest.mark.parametrize("kind", ["orb", "vwap"])
def test_pending_order_blocks_even_with_unused_entry_budget(
    calendar: MarketCalendar, kind: str
) -> None:
    now = OPEN + timedelta(minutes=90)
    data = (
        make_input(calendar, {"SPY": bars_from(orb_rows())}, now)
        if kind == "orb"
        else inp(calendar, stretched(), now_min=90)
    )
    strategy = (
        orb(max_entries_per_symbol_per_day=2)
        if kind == "orb"
        else vwap(max_entries_per_symbol_per_day=2)
    )
    data = replace(
        data, entry_history={(strategy.strategy_id, "SPY"): models.EntryHistory(pending=True)}
    )
    assert strategy.evaluate(data) == []
    assert strategy.entry_diagnostics["SPY"] == "ORDER_PENDING"


@pytest.mark.parametrize("factory", [orb, vwap])
@pytest.mark.parametrize(
    "bad", [{"max_entries_per_symbol_per_day": 0}, {"reentry_cooldown_minutes": -1}]
)
def test_invalid_reentry_parameters_fail_validation(factory, bad) -> None:
    with pytest.raises(ValueError):
        factory(**bad)


def test_reentry_limit_never_blocks_an_exit(calendar: MarketCalendar) -> None:
    from tests.unit.quant.test_orb import exit_input

    data = exit_input(calendar, "99.85")
    data = replace(data, entry_history={("orb_v1", "SPY"): models.EntryHistory(entries=2)})
    (signal,) = orb(max_entries_per_symbol_per_day=2).evaluate(data)
    assert signal.side.value == "SELL"


def test_legacy_history_cannot_bypass_cooldown(calendar: MarketCalendar) -> None:
    # A caller with only the old entered set cannot prove a full exit happened.
    data = make_input(
        calendar,
        {"SPY": bars_from(orb_rows())},
        OPEN + timedelta(minutes=90),
        entered=frozenset({("orb_v1", "SPY")}),
    )
    assert orb(max_entries_per_symbol_per_day=2).evaluate(data) == []
