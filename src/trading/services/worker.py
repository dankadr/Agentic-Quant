"""The trading worker: one active executor, guarded by a fenced lease.

Exit codes: 0 clean stop, 1 startup failure, 3 lease lost or database failure, 4 an approved
agent-config change was applied (restart to load it). A non-zero exit
lets the container supervisor restart the process, which re-acquires the lease and reconciles
before processing anything.
"""

import threading
import time
from collections.abc import Callable, Sequence
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.effective_config import config_changed
from trading.core.clock import Clock
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
from trading.services.exit_codes import (
    EXIT_CONFIG_RELOAD,
    EXIT_LEASE_LOST,
    EXIT_OK,
    EXIT_STARTUP_FAILED,
)
from trading.services.handlers import WorkerContext, build_handlers

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
    config_check_interval: float = 30.0,
    config_version: str | None = None,
    startup_hooks: Sequence[Callable[[WorkerContext], None]] = (),
    handlers_factory: Callable[[WorkerContext], dict[str, Handler]] = build_handlers,
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
    )
    renew_every = lease_ttl / 3
    last_renew = clock.now()
    last_config_check = time.monotonic()
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
            if time.monotonic() - last_config_check >= config_check_interval:
                last_config_check = time.monotonic()
                with session_factory() as session:
                    reload = config_changed(session, config_version)
                if reload:  # between jobs, never during one
                    _log.info("worker_config_reload", worker_id=worker_id)
                    LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
                    _release(session_factory, lease)
                    return EXIT_CONFIG_RELOAD
            if not worked:
                stop.wait(poll)
    except (SQLAlchemyError, LeaseLost):
        # Cannot prove we still hold the lease: stop processing and let the supervisor restart us.
        _log.critical("worker_database_failure", worker_id=worker_id, exc_info=True)
        LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
        return EXIT_LEASE_LOST
    LEASE_HELD.labels(EXECUTOR_LEASE).set(0)
    _release(session_factory, lease)
    _log.info("worker_stopped", worker_id=worker_id)
    return EXIT_OK
