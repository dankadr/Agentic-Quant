"""The trading worker: one active executor, guarded by a fenced lease.

Exit codes: 0 clean stop, 1 startup failure, 3 lease lost or database failure. A non-zero exit
lets the container supervisor restart the process, which re-acquires the lease and reconciles
before processing anything.
"""

import threading
from collections.abc import Callable, Sequence
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.jobs.lease import EXECUTOR_LEASE, Lease, acquire_lease, release_lease, renew_lease
from trading.jobs.outbox import dispatch_pending
from trading.jobs.runner import JobRunner
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD
from trading.services.handlers import WorkerContext, build_handlers

EXIT_OK = 0
EXIT_STARTUP_FAILED = 1
EXIT_LEASE_LOST = 3
_log = get_logger(__name__)


def _acquire(
    session_factory: sessionmaker[Session],
    clock: Clock,
    worker_id: str,
    ttl: timedelta,
    stop: threading.Event,
    retry: float,
) -> Lease | None:
    while not stop.is_set():
        try:
            with session_factory.begin() as session:
                lease = acquire_lease(
                    session, name=EXECUTOR_LEASE, holder=worker_id, now=clock.now(), ttl=ttl
                )
            if lease is not None:
                return lease
        except SQLAlchemyError as exc:
            _log.warning("lease_acquire_failed", error=type(exc).__name__)
        stop.wait(retry)
    return None


def _release(session_factory: sessionmaker[Session], lease: Lease) -> None:
    try:
        with session_factory.begin() as session:
            release_lease(session, lease)
    except SQLAlchemyError:
        _log.warning("lease_release_failed")


def run_worker(
    *,
    session_factory: sessionmaker[Session],
    clock: Clock,
    worker_id: str,
    ctx_factory: Callable[[Lease], WorkerContext],
    stop: threading.Event,
    lease_ttl: timedelta = timedelta(seconds=30),
    poll: float = 1.0,
    acquire_retry: float = 5.0,
    startup_hooks: Sequence[Callable[[WorkerContext], None]] = (),
) -> int:
    lease = _acquire(session_factory, clock, worker_id, lease_ttl, stop, acquire_retry)
    if lease is None:
        return EXIT_OK
    LEASE_HELD.labels(EXECUTOR_LEASE).set(1)
    try:
        ctx = ctx_factory(lease)
        for hook in startup_hooks:
            hook(ctx)
    except Exception:
        _log.exception("worker_startup_failed")
        LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
        _release(session_factory, lease)
        return EXIT_STARTUP_FAILED

    runner = JobRunner(
        session_factory, build_handlers(ctx), clock=clock, worker_id=worker_id, lease_for=lease_ttl
    )
    renew_every = lease_ttl / 3
    last_renew = clock.now()
    _log.info("worker_started", worker_id=worker_id, fencing_token=lease.fencing_token)
    try:
        while not stop.is_set():
            now = clock.now()
            if now - last_renew >= renew_every:
                with session_factory.begin() as session:
                    renewed = renew_lease(session, lease, now=now, ttl=lease_ttl)
                if renewed is None:
                    _log.critical("executor_lease_lost", worker_id=worker_id)
                    LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
                    return EXIT_LEASE_LOST
                last_renew = now
            worked = runner.run_once()
            with session_factory.begin() as session:
                dispatch_pending(session, {}, now=clock.now())
            if not worked:
                stop.wait(poll)
    except SQLAlchemyError:
        # Cannot prove we still hold the lease: stop processing and let the supervisor restart us.
        _log.critical("worker_database_failure", worker_id=worker_id, exc_info=True)
        LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
        return EXIT_LEASE_LOST
    LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
    _release(session_factory, lease)
    _log.info("worker_stopped", worker_id=worker_id)
    return EXIT_OK
