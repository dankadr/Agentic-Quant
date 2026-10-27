"""The trading worker: one active executor, guarded by a fenced lease.

Exit codes: 0 clean stop, 1 startup failure, 3 lease lost or database failure. A non-zero exit
lets the container supervisor restart the process, which re-acquires the lease and reconciles
before processing anything.

The executor lease is renewed on its own thread, so a long-running job cannot starve the renewal.
If renewal fails the loop takes no new job, and any in-flight write is rejected by its fence.
"""

import threading
import time
from collections.abc import Callable, Collection, Sequence
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.jobs.keepalive import Keepalive
from trading.jobs.kinds import EXECUTION_KINDS, OPERATIONS_KINDS
from trading.jobs.lease import (
    EXECUTOR_LEASE,
    Lease,
    LeaseLost,
    acquire_lease,
    release_lease,
    renew_lease,
)
from trading.jobs.outbox import dispatch_pending
from trading.jobs.runner import Handler, JobRunner
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD
from trading.services.exit_codes import EXIT_LEASE_LOST, EXIT_OK, EXIT_STARTUP_FAILED
from trading.services.handlers import WorkerContext, build_handlers

_log = get_logger(__name__)
RENEW_EVERY_SECONDS = 5.0  # the executor lease is renewed at least this often


def _renew_interval(ttl: timedelta) -> float:
    return min(RENEW_EVERY_SECONDS, ttl.total_seconds() / 3)


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


def _renew(
    session_factory: sessionmaker[Session], clock: Clock, lease: Lease, ttl: timedelta
) -> bool:
    with session_factory.begin() as session:
        return renew_lease(session, lease, now=clock.now(), ttl=ttl) is not None


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
    handlers_factory: Callable[[WorkerContext], dict[str, Handler]] = build_handlers,
    kinds: Collection[str] = EXECUTION_KINDS | OPERATIONS_KINDS,
    supervisor_factory: Callable[[WorkerContext], Callable[[], None]] | None = None,
    supervise_every: float = 5.0,
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
        session_factory,
        handlers_factory(ctx),
        clock=clock,
        worker_id=worker_id,
        lease_for=lease_ttl,
        kinds=kinds,
    )
    keeper = Keepalive(
        "executor-lease",
        lambda: _renew(session_factory, clock, lease, lease_ttl),
        interval=_renew_interval(lease_ttl),
        ttl=lease_ttl.total_seconds(),
    ).start()
    supervise = supervisor_factory(ctx) if supervisor_factory is not None else None
    next_supervise = 0.0
    _log.info("worker_started", worker_id=worker_id, fencing_token=lease.fencing_token)
    try:
        while not stop.is_set() and not keeper.lost.is_set():
            # Operator controls and exits run on their own short cycle, not behind the job queue.
            if supervise is not None and time.monotonic() >= next_supervise:
                supervise()
                next_supervise = time.monotonic() + supervise_every
            worked = runner.run_once()
            with session_factory.begin() as session:
                dispatch_pending(session, {}, now=clock.now())
            if not worked:
                stop.wait(poll)
    except (SQLAlchemyError, LeaseLost):
        # Cannot prove we still hold the lease: stop processing and let the supervisor restart us.
        _log.critical("worker_database_failure", worker_id=worker_id, exc_info=True)
        LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
        return EXIT_LEASE_LOST
    finally:
        keeper.stop()
    if keeper.lost.is_set():
        _log.critical("executor_lease_lost", worker_id=worker_id)
        LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
        return EXIT_LEASE_LOST
    LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
    _release(session_factory, lease)
    _log.info("worker_stopped", worker_id=worker_id)
    return EXIT_OK
