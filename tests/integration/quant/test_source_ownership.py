from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill, new_order
from tests.integration.quant.test_source import NOW, World
from trading.data.calendar import MarketCalendar
from trading.portfolio.ledger import apply_fill


@pytest.fixture
def world(db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar) -> World:
    return World(db, session_factory, calendar)


def test_other_strategy_skips_pending_ticker_until_cancellation(world: World) -> None:
    world.orb_day()
    order = new_order(world.db, world.account_id, "vwap-entry", strategy_ids=["vwap_v1"])
    assert world.propose() == ()
    assert world.count("select count(*) from signals where strategy_id='orb_v1'") == 0
    order.status = "CANCELED"
    world.db.flush()
    (request,) = world.propose()
    assert request.strategy_ids == ("orb_v1",)


def test_other_strategy_skips_filled_position_after_entry_is_terminal(world: World) -> None:
    world.orb_day()
    order = new_order(world.db, world.account_id, "vwap-entry", qty="0.2", strategy_ids=["vwap_v1"])
    apply_fill(
        world.db,
        account_id=world.account_id,
        fill=broker_fill(order, "vwap-fill", qty="0.2", price="100"),
        settles_on=NEXT_DAY,
        now=NOW,
    )
    order.status = "FILLED"
    world.db.flush()
    assert world.propose() == ()
    assert world.count("select count(*) from signals where strategy_id='orb_v1'") == 0
    assert world.db.execute(text("select qty from positions")).scalar_one() > 0


def test_owned_ticker_is_reported_as_reserved_instead_of_bad_market_data(world: World) -> None:
    world.orb_day()
    world.bar("SPY", 19, "100.30", "100.40", "100.20", "100.35", "1500")
    world.quote("SPY", NOW + timedelta(minutes=4, seconds=-6), "100.34", "100.36")
    new_order(world.db, world.account_id, "reserved", strategy_ids=["vwap_v1"])
    assert world.propose(NOW + timedelta(minutes=4)) == ()
    assert (
        world.count(
            "select count(*) from allocation_decisions, jsonb_array_elements(rationale_json) n "
            "where n->>'strategy_id'='orb_v1' and n->>'symbol'='SPY' "
            "and n->>'reason'='SYMBOL_OWNED'"
        )
        == 1
    )
