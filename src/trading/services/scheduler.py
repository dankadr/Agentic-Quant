"""The scheduler: a single leader that plans and enqueues durable jobs."""

import threading
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.core.config import AgentsConfig
from trading.data.calendar import MarketCalendar
from trading.jobs.keepalive import Keepalive
from trading.jobs.lease import SCHEDULER_LEASE, Lease, acquire_lease, release_lease, renew_lease
from trading.jobs.queue import enqueue
from trading.jobs.schedule import plan_jobs
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD
from trading.services.exit_codes import EXIT_LEASE_LOST, EXIT_OK

_log = get_logger(__name__)


def tick(
    session_factory: sessionmaker[Session],
    clock: Clock,
    calendar: MarketCalendar | None,
    agents: AgentsConfig | None = None,
) -> None:
    """Plan and enqueue the jobs due now. Dedupe keys make this idempotent."""
    for spec in plan_jobs(clock.now(), calendar, agents=agents):
        with session_factory.begin() as session:
            enqueue(
                session,
                kind=spec.kind,
                payload=spec.payload,
                scheduled_at=spec.scheduled_at,
                dedupe_key=spec.dedupe_key,
            )


def _renew(
    session_factory: sessionmaker[Session], clock: Clock, lease: Lease, ttl: timedelta
) -> bool:
    with session_factory.begin() as session:
        return renew_lease(session, lease, now=clock.now(), ttl=ttl) is not None


def run_scheduler(
    *,
    session_factory: sessionmaker[Session],
    clock: Clock,
    holder: str,
    calendar: MarketCalendar | None,
    stop: threading.Event,
    agents: AgentsConfig | None = None,
    lease_ttl: timedelta = timedelta(seconds=30),
    interval: float = 10.0,
    acquire_retry: float = 5.0,
) -> int:
    lease = None
    while lease is None and not stop.is_set():
        try:
            with session_factory.begin() as session:
                lease = acquire_lease(
                    session, name=SCHEDULER_LEASE, holder=holder, now=clock.now(), ttl=lease_ttl
                )
        except SQLAlchemyError as exc:
            _log.warning("scheduler_lease_acquire_failed", error=type(exc).__name__)
        if lease is None:
            stop.wait(acquire_retry)
    if lease is None:
        return EXIT_OK
    LEASE_HELD.labels(SCHEDULER_LEASE).set(1)
    _log.info("scheduler_started", holder=holder, fencing_token=lease.fencing_token)
    keeper = Keepalive(
        "scheduler-lease",
        lambda: _renew(session_factory, clock, lease, lease_ttl),
        interval=min(5.0, lease_ttl.total_seconds() / 3),
        ttl=lease_ttl.total_seconds(),
    ).start()
    try:
        while not stop.is_set() and not keeper.lost.is_set():
            tick(session_factory, clock, calendar, agents)
            stop.wait(interval)
    except SQLAlchemyError:
        _log.critical("scheduler_database_failure", exc_info=True)
        LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
        return EXIT_LEASE_LOST
    finally:
        keeper.stop()
    if keeper.lost.is_set():
        _log.critical("scheduler_lease_lost", holder=holder)
        LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
        return EXIT_LEASE_LOST
    LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
    try:
        with session_factory.begin() as session:
            release_lease(session, lease)
    except SQLAlchemyError:
        _log.warning("scheduler_lease_release_failed")
    return EXIT_OK
