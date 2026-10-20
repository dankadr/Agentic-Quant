import threading
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.services.helpers import TTL, lease_holder, wait_until
from trading.core.clock import ManualClock, SystemClock
from trading.jobs.lease import SCHEDULER_LEASE, acquire_lease
from trading.services.scheduler import run_scheduler, tick


def _job_count(sf: sessionmaker[Session]) -> int:
    with sf() as s:
        return s.execute(text("select count(*) from jobs where kind = 'heartbeat'")).scalar_one()


def test_tick_enqueues_once_per_minute(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
) -> None:
    tick(session_factory, clock, None)
    tick(session_factory, clock, None)
    assert _job_count(session_factory) == 1
    clock.advance(timedelta(minutes=1))
    tick(session_factory, clock, None)
    assert _job_count(session_factory) == 2


def _start(sf: sessionmaker[Session], holder: str, clock: object, stop: threading.Event):  # type: ignore[no-untyped-def]
    result: dict[str, int] = {}

    def target() -> None:
        result["code"] = run_scheduler(
            session_factory=sf,
            clock=clock,  # type: ignore[arg-type]
            holder=holder,
            calendar=None,
            stop=stop,
            lease_ttl=TTL,
            interval=0.1,
            acquire_retry=0.1,
        )

    t = threading.Thread(target=target, daemon=True)
    t.start()
    return t, result


def test_scheduler_loop_enqueues_and_releases_lease_on_stop(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    stop = threading.Event()
    t, result = _start(session_factory, "s1", SystemClock(), stop)
    assert wait_until(lambda: _job_count(session_factory) >= 1)
    assert lease_holder(session_factory, SCHEDULER_LEASE) == "s1"
    stop.set()
    t.join(10)
    assert result["code"] == 0
    assert lease_holder(session_factory, SCHEDULER_LEASE) is None


def test_second_scheduler_waits_while_leader_holds_lease(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        acquire_lease(
            s,
            name=SCHEDULER_LEASE,
            holder="leader",
            now=SystemClock().now(),
            ttl=timedelta(hours=1),
        )
    stop = threading.Event()
    t, result = _start(session_factory, "s2", SystemClock(), stop)
    stop.wait(0.5)
    assert _job_count(session_factory) == 0
    stop.set()
    t.join(10)
    assert result["code"] == 0


def test_scheduler_exits_when_lease_taken_over(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    stop = threading.Event()
    t, result = _start(session_factory, "s1", SystemClock(), stop)
    assert wait_until(lambda: lease_holder(session_factory, SCHEDULER_LEASE) == "s1")
    with session_factory.begin() as s:
        s.execute(
            text(
                "update leases set holder = 'thief', fencing_token = fencing_token + 1,"
                " expires_at = now() + interval '1 hour' where name = 'scheduler'"
            )
        )
    t.join(10)
    assert not t.is_alive()
    assert result["code"] == 3
