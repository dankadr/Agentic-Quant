"""Single-threaded job runner: claim, execute, then complete or fail (separate transactions)."""

from collections.abc import Callable, Mapping
from datetime import timedelta

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.jobs.lease import LeaseLost
from trading.jobs.queue import Job, claim, complete, fail
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
    ) -> None:
        self._sf = session_factory
        self._handlers = handlers
        self._clock = clock
        self._worker_id = worker_id
        self._lease_for = lease_for

    def run_once(self) -> bool:
        """Process at most one job. Returns False when nothing was due."""
        with self._sf.begin() as session:
            job = claim(
                session,
                worker_id=self._worker_id,
                now=self._clock.now(),
                lease_for=self._lease_for,
            )
        if job is None:
            return False
        handler = self._handlers.get(job.kind)
        error: str | None = None
        if handler is None:
            error = f"no handler for kind {job.kind!r}"
        else:
            try:
                handler(job)
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
