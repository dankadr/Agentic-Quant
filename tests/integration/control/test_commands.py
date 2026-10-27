"""Operator commands end to end against the paper environment: entries stop, orders cancel, and a
flatten completes only once the broker shows no open orders and no held shares."""

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import build_env
from trading.control.commands import (
    CommandKind,
    CommandNotAllowed,
    CommandStatus,
    get_command,
    request_command,
)
from trading.control.state import RiskState, TradingState, get_system_state
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
