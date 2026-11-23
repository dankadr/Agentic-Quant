import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import Env, build_env
from trading.core.clock import ManualClock
from trading.core.enums import PositionEffect, Side
from trading.core.errors import TradingError
from trading.data.calendar import MarketCalendar


@pytest.fixture
def env(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> Env:
    return build_env(db, session_factory, clock, calendar)


def test_acceptance_rechecks_owner_before_recording_a_conflicting_entry(env: Env) -> None:
    env.place(strategy="orb_v1")
    with pytest.raises(TradingError, match="SYMBOL_OWNED"):
        env.place(strategy="vwap_v1")
    assert env.scalar("select count(*) from orders") == 1


def test_acceptance_rejects_shared_entry_even_when_ticker_is_free(env: Env) -> None:
    with pytest.raises(TradingError, match="SYMBOL_OWNED"):
        env.place(strategy_ids=("orb_v1", "vwap_v1"))
    assert env.scalar("select count(*) from orders") == 0


def test_acceptance_releases_unfilled_terminal_order(env: Env) -> None:
    first = env.place(strategy="orb_v1")
    with env.sf.begin() as session:
        session.execute(text("update orders set status='CANCELED' where id=:id"), {"id": first})
    env.place(strategy="vwap_v1")
    assert env.scalar("select count(*) from orders") == 2


def test_other_strategy_cannot_close_the_owners_ticker(env: Env) -> None:
    env.place(strategy="orb_v1")
    with pytest.raises(TradingError, match="SYMBOL_OWNED"):
        env.place(strategy="vwap_v1", side=Side.SELL)
    assert env.scalar("select count(*) from orders") == 1


def test_safety_flatten_can_close_a_reserved_ticker(env: Env) -> None:
    env.place(strategy="orb_v1")
    env.place(strategy="eod_flatten", side=Side.SELL)
    assert env.scalar("select count(*) from orders") == 2


def test_safety_flatten_with_owner_as_primary_can_exit_unattributed_shares(env: Env) -> None:
    env.place(strategy="orb_v1")
    env.place(strategy="orb_v1", strategy_ids=("orb_v1", "eod_flatten"), side=Side.SELL)
    assert env.scalar("select count(*) from orders") == 2


def test_short_entry_reserves_ticker_and_owner_can_cover(env: Env) -> None:
    env.place(strategy="orb_v1", side=Side.SELL, effect=PositionEffect.OPEN_SHORT)
    with pytest.raises(TradingError, match="SYMBOL_OWNED"):
        env.place(strategy="vwap_v1", side=Side.SELL, effect=PositionEffect.OPEN_SHORT)
    with pytest.raises(TradingError, match="SYMBOL_OWNED"):
        env.place(strategy="vwap_v1", side=Side.BUY, effect=PositionEffect.CLOSE_SHORT)
    env.place(strategy="orb_v1", side=Side.BUY, effect=PositionEffect.CLOSE_SHORT)
    assert env.scalar("select count(*) from orders") == 2
