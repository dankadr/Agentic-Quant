"""Acceptance test 6: a broker timeout becomes UNKNOWN; after a restart, reconciliation resolves it
without ever creating a duplicate order."""

from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.execution.env import CFG, NOW, D, SpyBroker, build_env
from trading.brokers.paper.broker import PaperBroker
from trading.brokers.paper.faults import FaultPlan
from trading.control.state import RiskState, get_system_state
from trading.core.clock import ManualClock
from trading.execution.manager import ExecutionManager
from trading.execution.reconcile import reconcile_account
from trading.execution.state_machine import OrderStatus
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease

COID = "paper-orb_v1-20261019-0001"


def test_timeout_then_restart_reconciles_without_duplicate(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock, calendar
) -> None:  # type: ignore[no-untyped-def]
    env = build_env(
        db,
        session_factory,
        clock,
        calendar,
        faults=FaultPlan(timeout_after_accept=frozenset({COID})),
    )
    order_id = env.place()
    assert env.manager.submit(order_id) is OrderStatus.UNKNOWN

    # ---- "crash": drop every in-memory object; the new process takes over the lease ----
    del env.manager, env.spy, env.broker
    with session_factory.begin() as s:
        s.execute(
            text("update leases set expires_at = :past"), {"past": NOW - timedelta(seconds=1)}
        )
    with session_factory.begin() as s:
        new_lease = acquire_lease(
            s, name=EXECUTOR_LEASE, holder="w2", now=NOW, ttl=timedelta(hours=1)
        )
    assert new_lease is not None
    assert new_lease.fencing_token == env.lease.fencing_token + 1
    instruments = env.instruments
    fresh_broker = PaperBroker(session_factory, account_ref="paper-primary", config=CFG.paper_broker,
                               instruments=instruments, clock=clock)  # fmt: skip
    spy = SpyBroker(fresh_broker)

    report = reconcile_account(session_factory, account_id=env.account_id, broker=spy, clock=clock,
                               lease=new_lease, calendar=calendar, config=CFG)  # fmt: skip
    assert report.status == "OK"
    assert env.status(order_id) == "ACKNOWLEDGED"
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.NORMAL

    manager_b = ExecutionManager(
        session_factory, spy, clock=clock, lease=new_lease, calendar=calendar, config=CFG
    )  # type: ignore[arg-type]
    assert (
        manager_b.submit(order_id) is OrderStatus.ACKNOWLEDGED
    )  # already acknowledged: no broker call
    assert spy.submit_calls == 0

    env.tick_with(fresh_broker) if hasattr(env, "tick_with") else None
    clock.advance(timedelta(seconds=1))
    from trading.data.models import Bar, Quote

    now = clock.now()
    fresh_broker.process_tick(
        quotes={"SPY": Quote(symbol="SPY", ts=now, bid=D("99.99"), ask=D("100.00"), bid_size=None, ask_size=None, provider="p")},
        bars={"SPY": Bar(symbol="SPY", interval="1m", ts=now - timedelta(minutes=1), open=D(100), high=D(101), low=D(99),
                         close=D(100), volume=D("100000"), provider="p", source_seq=None)},
    )  # fmt: skip
    final = reconcile_account(session_factory, account_id=env.account_id, broker=spy, clock=clock,
                              lease=new_lease, calendar=calendar, config=CFG)  # fmt: skip
    assert final.status == "OK"
    assert env.status(order_id) == "FILLED"
    with session_factory() as s:
        venue_orders = s.execute(
            text("select count(*) from paper_venue.venue_orders where client_order_id = :c"),
            {"c": COID},
        ).scalar_one()
        positions = s.execute(text("select qty from positions")).scalar_one()
        venue_position = fresh_broker.get_account().positions
    assert venue_orders == 1
    assert dict(venue_position) == {"SPY": positions}
    assert spy.submit_calls == 0
