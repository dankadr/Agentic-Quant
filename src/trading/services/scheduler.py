"""The scheduler: a single leader that plans and enqueues durable jobs."""

import threading
import time
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.effective_config import apply_when_idle, config_changed_since
from trading.core.clock import Clock
from trading.core.config import AgentsConfig
from trading.data.calendar import MarketCalendar
from trading.jobs.lease import SCHEDULER_LEASE, acquire_lease, release_lease, renew_lease
from trading.jobs.queue import enqueue
from trading.jobs.schedule import plan_jobs
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD
from trading.services.exit_codes import EXIT_CONFIG_RELOAD, EXIT_LEASE_LOST, EXIT_OK

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
    config_check_interval: float = 30.0,
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
    started_at = last_renew = clock.now()
    last_config_check = time.monotonic()
    try:
        while not stop.is_set():
            now = clock.now()
            if now - last_renew >= lease_ttl / 3:
                with session_factory.begin() as session:
                    renewed = renew_lease(session, lease, now=now, ttl=lease_ttl)
                if renewed is None:
                    _log.critical("scheduler_lease_lost", holder=holder)
                    LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
                    return EXIT_LEASE_LOST
                last_renew = now
            tick(session_factory, clock, calendar, agents)
            if time.monotonic() - last_config_check >= config_check_interval:
                last_config_check = time.monotonic()
                if calendar is not None:
                    with session_factory.begin() as session:
                        apply_when_idle(session, clock, calendar)
                with session_factory() as session:
                    reload = config_changed_since(session, started_at)
                if reload:
                    _log.info("scheduler_config_reload", holder=holder)
                    LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
                    with session_factory.begin() as session:
                        release_lease(session, lease)
                    return EXIT_CONFIG_RELOAD
            stop.wait(interval)
    except SQLAlchemyError:
        _log.critical("scheduler_database_failure", exc_info=True)
        LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
        return EXIT_LEASE_LOST
    LEASE_HELD.labels(SCHEDULER_LEASE).set(0)
    try:
        with session_factory.begin() as session:
            release_lease(session, lease)
    except SQLAlchemyError:
        _log.warning("scheduler_lease_release_failed")
    return EXIT_OK
