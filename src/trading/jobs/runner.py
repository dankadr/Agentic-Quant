"""Single-threaded job runner: claim, execute, then complete or fail (separate transactions).

While a handler runs, its job lease is renewed on a background thread. A claim whose renewal
fails has been reclaimed by someone else, so its eventual ``complete``/``fail`` matches nothing and
its result is dropped rather than recorded.
"""

from collections.abc import Callable, Collection, Mapping
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.jobs.keepalive import Keepalive
from trading.jobs.lease import LeaseLost
from trading.jobs.queue import Job, claim, complete, fail, renew
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import JOBS_PROCESSED

Handler = Callable[[Job], None]
_log = get_logger(__name__)


class JobRunner:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        handlers: Mapping[str, Handler],
        *,
        clock: Clock,
        worker_id: str,
        lease_for: timedelta = timedelta(minutes=5),
        kinds: Collection[str] | None = None,
    ) -> None:
        self._sf = session_factory
        self._handlers = handlers
        self._clock = clock
        self._worker_id = worker_id
        self._lease_for = lease_for
        self._kinds = None if kinds is None else frozenset(kinds)

    def run_once(self) -> bool:
        """Process at most one job. Returns False when nothing was due."""
        with self._sf.begin() as session:
            job = claim(
                session,
                worker_id=self._worker_id,
                now=self._clock.now(),
                lease_for=self._lease_for,
                kinds=self._kinds,
            )
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        error: str | None = None
        if handler is None:
            error = f"no handler for kind {job.kind!r}"
        else:
            try:
                self._run_with_lease(job, handler)
            except (LeaseLost, SQLAlchemyError):
                # Fenced out, or the database is failing: stop the worker; the job's lease
                # expires and it is reclaimed. Never record this as an ordinary job failure.
                raise
            except Exception as exc:
                _log.exception("job_failed", job_id=str(job.id), kind=job.kind)
                error = f"{type(exc).__name__}: {exc}"
        with self._sf.begin() as session:
            now = self._clock.now()
            if error is None:
                owned = complete(
                    session, job.id, worker_id=self._worker_id, attempts=job.attempts, now=now
                )
            else:
                owned = (
                    fail(
                        session,
                        job.id,
                        worker_id=self._worker_id,
                        attempts=job.attempts,
                        error=error,
                        now=now,
                    )
                    is not None
                )
            JOBS_PROCESSED.labels(job.kind, "succeeded" if error is None else "failed").inc()
            if not owned:
                _log.warning("job_claim_lost", job_id=str(job.id), kind=job.kind)
        return True

    def _run_with_lease(self, job: Job, handler: Handler) -> None:
        keeper = Keepalive(
            f"job-{job.id}",
            lambda: self._renew_job(job),
            interval=self._lease_for.total_seconds() / 3,
            ttl=self._lease_for.total_seconds(),
        ).start()
        try:
            handler(job)
        finally:
            keeper.stop()
            if keeper.lost.is_set():
                _log.warning("job_lease_lost", job_id=str(job.id), kind=job.kind)

    def _renew_job(self, job: Job) -> bool:
        with self._sf.begin() as session:
            return renew(
                session,
                job.id,
                worker_id=self._worker_id,
                attempts=job.attempts,
                now=self._clock.now(),
                lease_for=self._lease_for,
            )
