"""Operator commands end to end against the paper environment: entries stop, orders cancel, and a
flatten completes only once the broker shows no open orders and no held shares."""

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import build_env
from trading.control.commands import (
    CommandKind,
    CommandNotAllowed,
    CommandStatus,
    get_command,
    request_command,
    resume_refusal,
)
from trading.control.state import RiskState, TradingState, get_system_state, set_trading_state
from trading.core.clock import ManualClock
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.execution.controls import ControlSupervisor


def _order_status(db: Session, order_id) -> str:  # type: ignore[no-untyped-def]
    db.rollback()
    return db.execute(
        text("select status from orders where id = :id"), {"id": str(order_id)}
    ).scalar_one()


def _request(session_factory: sessionmaker[Session], clock: ManualClock, kind: CommandKind):  # type: ignore[no-untyped-def]
    with session_factory.begin() as s:
        version = get_system_state(s).version
        return request_command(
            s,
            kind=kind,
            actor="operator",
            reason="test",
            expected_version=version,
            request_id=None,
            now=clock.now(),
        )


def _supervise(env) -> None:  # type: ignore[no-untyped-def]
    ControlSupervisor(
        env.sf, env.manager, clock=env.clock, lease=env.lease, account_id=env.account_id
    ).run_cycle()


def test_flatten_cancels_the_entry_and_ends_in_exit_only(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    env = build_env(db, session_factory, clock, calendar)
    order_id = env.place(symbol="SPY", side=Side.BUY, qty="0.5", limit="1.00")  # far below market
    env.manager.submit(order_id)
    assert _order_status(db, order_id) == "ACKNOWLEDGED"

    command, state = _request(session_factory, clock, CommandKind.FLATTEN)
    assert state.trading_state is TradingState.FLATTENING

    _supervise(env)

    assert _order_status(db, order_id) == "CANCELED"
    with session_factory() as s:
        view = get_command(s, command.id)
        assert view.status is CommandStatus.COMPLETED
        assert get_system_state(s).trading_state is TradingState.EXIT_ONLY


def test_stop_entries_completes_once_entry_orders_are_gone(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    env = build_env(db, session_factory, clock, calendar)
    order_id = env.place(symbol="SPY", side=Side.BUY, qty="0.5", limit="1.00")
    env.manager.submit(order_id)

    command, state = _request(session_factory, clock, CommandKind.STOP_ENTRIES)
    assert state.trading_state is TradingState.EXIT_ONLY

    _supervise(env)

    assert _order_status(db, order_id) == "CANCELED"
    with session_factory() as s:
        assert get_command(s, command.id).status is CommandStatus.COMPLETED


def test_no_new_entry_reaches_the_broker_once_entries_are_stopped(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    env = build_env(db, session_factory, clock, calendar)
    _request(session_factory, clock, CommandKind.STOP_ENTRIES)
    before = env.spy.submit_calls

    order_id = env.place(symbol="SPY", side=Side.BUY, qty="0.5", limit="1.00")
    env.manager.submit(order_id)

    assert env.spy.submit_calls == before
    assert _order_status(db, order_id) == "REJECTED"


def test_a_stop_request_never_downgrades_an_in_progress_flatten(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    build_env(db, session_factory, clock, calendar)
    _request(session_factory, clock, CommandKind.FLATTEN)
    _, state = _request(session_factory, clock, CommandKind.STOP_ENTRIES)
    assert state.trading_state is TradingState.FLATTENING


def test_a_stop_request_never_lifts_a_pause(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """Review finding: stop-entries from PAUSED used to move to EXIT_ONLY, unfreezing sells."""
    build_env(db, session_factory, clock, calendar, running=False)
    _, state = _request(session_factory, clock, CommandKind.STOP_ENTRIES)
    assert state.trading_state is TradingState.PAUSED


def test_cancel_open_orders_still_runs_while_paused(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """Cancelling is not submitting: a paused system can still cancel what is working."""
    env = build_env(db, session_factory, clock, calendar, running=False)
    _, state = _request(session_factory, clock, CommandKind.CANCEL_OPEN_ORDERS)
    assert state.trading_state is TradingState.PAUSED
    _supervise(env)  # must not raise; with nothing open it completes at once


def test_flatten_is_refused_while_the_risk_state_is_unknown(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    build_env(db, session_factory, clock, calendar, risk=RiskState.UNKNOWN)
    try:
        _request(session_factory, clock, CommandKind.FLATTEN)
    except CommandNotAllowed:
        pass
    else:  # pragma: no cover - the refusal is the behaviour under test
        raise AssertionError("flatten must be refused while the risk state is UNKNOWN")
    with session_factory() as s:
        assert get_system_state(s).trading_state is not TradingState.FLATTENING


def _hold_a_position(env) -> None:  # type: ignore[no-untyped-def]
    bought = env.place()
    env.manager.submit(bought)
    env.tick()
    env.manager.sync_fills()


def _pause(session_factory: sessionmaker[Session], clock: ManualClock) -> None:
    with session_factory.begin() as s:
        set_trading_state(
            s,
            target=TradingState.PAUSED,
            expected_version=get_system_state(s).version,
            actor="operator",
            reason="test",
            request_id=None,
            now=clock.now(),
        )


def test_cancel_open_orders_spares_an_exit_placed_after_the_request(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """Review finding: the command re-read open orders each cycle and cancelled later exits."""
    env = build_env(db, session_factory, clock, calendar)
    _hold_a_position(env)
    older = env.place(symbol="SPY", side=Side.BUY, qty="0.5", limit="1.00")
    env.manager.submit(older)

    command, _ = _request(session_factory, clock, CommandKind.CANCEL_OPEN_ORDERS)
    clock.advance(timedelta(seconds=30))
    later_exit = env.place(side=Side.SELL, limit="101.00")
    env.manager.submit(later_exit)

    _supervise(env)

    assert _order_status(db, older) == "CANCELED"
    assert _order_status(db, later_exit) == "ACKNOWLEDGED"
    with session_factory() as s:
        assert get_command(s, command.id).status is CommandStatus.COMPLETED


def test_cancel_open_orders_cancels_an_exit_already_working(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    env = build_env(db, session_factory, clock, calendar)
    _hold_a_position(env)
    working_exit = env.place(side=Side.SELL, limit="101.00")
    env.manager.submit(working_exit)

    _request(session_factory, clock, CommandKind.CANCEL_OPEN_ORDERS)
    clock.advance(timedelta(seconds=30))
    _supervise(env)

    assert _order_status(db, working_exit) == "CANCELED"


def test_a_pause_abandons_a_flatten_and_resume_works_afterwards(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """Sells cannot run while paused, so an open flatten would otherwise block resume forever."""
    env = build_env(db, session_factory, clock, calendar)
    _hold_a_position(env)
    command, _ = _request(session_factory, clock, CommandKind.FLATTEN)
    _pause(session_factory, clock)
    with session_factory() as s:
        assert resume_refusal(s, get_system_state(s)) is not None  # not closed yet

    _supervise(env)

    with session_factory() as s:
        view = get_command(s, command.id)
        assert view.status is CommandStatus.FAILED
        assert view.progress["superseded_by"] == "PAUSED"
        state = get_system_state(s)
        assert state.trading_state is TradingState.PAUSED
        assert resume_refusal(s, state) is None


def test_two_flatten_requests_both_complete(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """The first completion moves to EXIT_ONLY; that must not read as the second being replaced."""
    env = build_env(db, session_factory, clock, calendar)
    first, _ = _request(session_factory, clock, CommandKind.FLATTEN)
    second, _ = _request(session_factory, clock, CommandKind.FLATTEN)

    _supervise(env)

    with session_factory() as s:
        assert get_command(s, first.id).status is CommandStatus.COMPLETED
        assert get_command(s, second.id).status is CommandStatus.COMPLETED
        assert get_system_state(s).trading_state is TradingState.EXIT_ONLY
