from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.control.state import RiskState, get_system_state, set_risk_state
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.data.calendar import MarketCalendar
from trading.data.replay_provider import CsvReplayProvider
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease
from trading.jobs.outbox import emit_event
from trading.jobs.queue import Job, enqueue
from trading.jobs.runner import JobRunner
from trading.services.bootstrap import build_runtime
from trading.services.handlers import WorkerContext, build_handlers, reconcile_on_start

CFG = load_config(Path("configs"))
FIXTURE = Path("tests/fixtures/replay/2026-10-19")
TICK_TIME = datetime(2026, 10, 19, 13, 31, 5, tzinfo=UTC)


def make_ctx(
    db: Session,
    sf: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
    *,
    provider: bool = False,
    at: datetime = TICK_TIME,
) -> WorkerContext:
    clock.set(at)
    lease = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=at, ttl=timedelta(hours=1))
    assert lease is not None
    db.commit()
    csv = CsvReplayProvider(FIXTURE) if provider else None
    runtime = build_runtime(session_factory=sf, config=CFG, clock=clock, lease=lease,
                            calendar=calendar, provider=csv, provider_name="replay")  # fmt: skip
    return WorkerContext(
        settings=Settings(),
        config=CFG,
        session_factory=sf,
        clock=clock,
        lease=lease,
        runtime=runtime,
    )


def job(kind: str, **payload: str) -> Job:
    return Job(id=uuid4(), kind=kind, payload=payload, attempts=1, max_attempts=5)


def count(sf: sessionmaker[Session], sql: str) -> int:
    with sf() as s:
        return s.execute(text(sql)).scalar_one()


def test_handlers_registered(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    assert set(build_handlers(ctx)) == {
        "heartbeat", "session_start", "paper_tick", "session_end", "reconcile", "outbox_dispatch",
    }  # fmt: skip
    bare = WorkerContext(
        settings=ctx.settings,
        config=CFG,
        session_factory=session_factory,
        clock=clock,
        lease=ctx.lease,
    )
    assert set(build_handlers(bare)) == {"heartbeat"}


def test_session_start_opens_the_session_and_reconciles(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    build_handlers(ctx)["session_start"](job("session_start", session_date="2026-10-19"))
    assert count(session_factory, "select count(*) from trading_sessions") == 1
    assert (
        count(session_factory, "select count(*) from equity_snapshots where kind = 'SESSION_START'")
        == 1
    )
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.NORMAL


def test_paper_tick_without_a_provider_does_nothing(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar, provider=False)
    build_handlers(ctx)["paper_tick"](job("paper_tick", minute="2026-10-19T13:31:00Z"))
    assert count(session_factory, "select count(*) from market_snapshots") == 0
    assert count(session_factory, "select count(*) from equity_snapshots") == 0


def test_paper_tick_with_a_provider_runs_a_full_tick(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar, provider=True)
    build_handlers(ctx)["paper_tick"](job("paper_tick", minute="2026-10-19T13:31:00Z"))
    assert count(session_factory, "select count(*) from market_snapshots") == 1
    assert (
        count(session_factory, "select count(*) from equity_snapshots where kind = 'INTRADAY'") == 1
    )
    assert count(session_factory, "select count(*) from market_bars") == 3  # one bar per symbol
    assert (
        count(session_factory, "select count(*) from trading_sessions") == 1
    )  # started late, automatically


def test_a_stale_tick_is_skipped_and_audited(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(
        db,
        session_factory,
        clock,
        calendar,
        provider=True,
        at=datetime(2026, 10, 19, 13, 33, 0, tzinfo=UTC),
    )
    build_handlers(ctx)["paper_tick"](job("paper_tick", minute="2026-10-19T13:31:00Z"))  # 120 s old
    assert count(session_factory, "select count(*) from market_snapshots") == 0
    assert (
        count(
            session_factory, "select count(*) from audit_events where action = 'tick_skipped_stale'"
        )
        == 1
    )


def test_a_tick_exactly_at_the_limit_still_runs(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(
        db,
        session_factory,
        clock,
        calendar,
        provider=True,
        at=datetime(2026, 10, 19, 13, 32, 30, tzinfo=UTC),
    )
    build_handlers(ctx)["paper_tick"](
        job("paper_tick", minute="2026-10-19T13:31:00Z")
    )  # exactly 90 s
    assert count(session_factory, "select count(*) from market_snapshots") == 1


def test_session_end_and_reconcile_handlers(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(
        db,
        session_factory,
        clock,
        calendar,
        provider=True,
        at=datetime(2026, 10, 19, 20, 5, tzinfo=UTC),
    )
    handlers = build_handlers(ctx)
    handlers["reconcile"](job("reconcile"))
    handlers["session_end"](job("session_end", session_date="2026-10-19"))
    assert (
        count(session_factory, "select count(*) from audit_events where action = 'session.end'")
        == 1
    )
    assert count(session_factory, "select count(*) from reconciliation_runs") >= 2


def test_outbox_dispatch_handler_delivers_pending_events(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    with session_factory.begin() as s:
        emit_event(s, event_type="note", payload={"n": 1}, now=clock.now())
    build_handlers(ctx)["outbox_dispatch"](job("outbox_dispatch"))
    assert count(session_factory, "select count(*) from outbox where delivered_at is null") == 0


def test_reconcile_on_start_clears_unknown_risk(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.UNKNOWN
    reconcile_on_start(ctx)
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.NORMAL


def test_reconcile_on_start_is_a_noop_without_a_runtime(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    bare = WorkerContext(
        settings=ctx.settings,
        config=CFG,
        session_factory=session_factory,
        clock=clock,
        lease=ctx.lease,
    )
    reconcile_on_start(bare)
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.UNKNOWN
    assert set_risk_state


def test_jobs_flow_through_the_runner(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar, provider=True)
    with session_factory.begin() as s:
        enqueue(
            s,
            kind="session_start",
            payload={"session_date": "2026-10-19"},
            scheduled_at=TICK_TIME - timedelta(seconds=5),
            dedupe_key="ss",
        )
        enqueue(
            s,
            kind="paper_tick",
            payload={"minute": "2026-10-19T13:31:00Z"},
            scheduled_at=TICK_TIME,
            dedupe_key="t1",
        )
    runner = JobRunner(session_factory, build_handlers(ctx), clock=clock, worker_id="w1")
    assert runner.run_once() is True
    assert runner.run_once() is True
    assert runner.run_once() is False
    assert count(session_factory, "select count(*) from jobs where state = 'SUCCEEDED'") == 2
    assert count(session_factory, "select count(*) from market_snapshots") == 1


@pytest.mark.parametrize("kind", ["session_start", "session_end"])
def test_bad_payload_fails_the_job_instead_of_crashing_the_worker(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
    kind: str,
) -> None:
    ctx = make_ctx(db, session_factory, clock, calendar)
    with session_factory.begin() as s:
        enqueue(
            s,
            kind=kind,
            payload={"session_date": "not-a-date"},
            scheduled_at=TICK_TIME,
            dedupe_key="bad",
        )
    runner = JobRunner(session_factory, build_handlers(ctx), clock=clock, worker_id="w1")
    assert runner.run_once() is True
    assert (
        count(
            session_factory,
            "select count(*) from jobs where state = 'QUEUED' and last_error is not null",
        )
        == 1
    )


# ---- final review: in-session reconciliation and fencing before venue mutations -----------------


def test_a_tick_heals_reconciliation_required_in_session(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    """Review I7: one transient failure must not block exits until the session ends."""
    ctx = make_ctx(db, session_factory, clock, calendar, provider=True)
    build_handlers(ctx)["session_start"](
        job("session_start", session_date="2026-10-19")
    )  # session open
    with session_factory.begin() as s:
        set_risk_state(
            s,
            risk_state=RiskState.RECONCILIATION_REQUIRED,
            reason="blip",
            actor="t",
            now=clock.now(),
        )
    build_handlers(ctx)["paper_tick"](job("paper_tick", minute="2026-10-19T13:31:00Z"))
    with session_factory() as s:
        assert get_system_state(s).risk_state is RiskState.NORMAL


def test_a_tick_resolves_orders_stuck_in_limbo(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    from tests.integration.portfolio.helpers import new_order

    ctx = make_ctx(db, session_factory, clock, calendar, provider=True)
    assert ctx.runtime is not None
    build_handlers(ctx)["session_start"](
        job("session_start", session_date="2026-10-19")
    )  # session already open
    db.rollback()
    new_order(
        db, ctx.runtime.account_id, "stuck-1", status="UNKNOWN"
    )  # created at T0, long past the grace
    db.commit()
    build_handlers(ctx)["paper_tick"](job("paper_tick", minute="2026-10-19T13:31:00Z"))
    assert (
        count(
            session_factory,
            "select count(*) from orders where status = 'REJECTED' and status_reason = 'NOT_FOUND_AT_BROKER'",
        )
        == 1
    )


def test_end_session_fences_before_touching_the_venue(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> None:
    from decimal import Decimal

    from trading.brokers.base import BrokerOrderRequest
    from trading.core.enums import OrderType, Side, TimeInForce
    from trading.jobs.lease import LeaseLost

    ctx = make_ctx(
        db,
        session_factory,
        clock,
        calendar,
        provider=True,
        at=datetime(
            2026, 10, 19, 19, 0, tzinfo=UTC
        ),  # the resting order is placed before the close
    )
    assert ctx.runtime is not None
    ctx.runtime.broker.submit_order(
        BrokerOrderRequest(
            "resting-1",
            "SPY",
            Side.BUY,
            OrderType.LIMIT,
            Decimal("0.5"),
            Decimal("1.00"),
            TimeInForce.DAY,
        )
    )
    clock.set(datetime(2026, 10, 19, 20, 5, tzinfo=UTC))
    with session_factory.begin() as s:
        s.execute(text("update leases set holder = 'thief', fencing_token = fencing_token + 1"))
    with pytest.raises(LeaseLost):
        ctx.runtime.processor.end_session(datetime(2026, 10, 19, tzinfo=UTC).date())
    assert (
        count(
            session_factory,
            "select count(*) from paper_venue.venue_orders where status = 'ACCEPTED'",
        )
        == 1
    )  # not expired
