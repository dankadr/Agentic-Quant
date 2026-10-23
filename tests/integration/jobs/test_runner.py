from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.jobs.queue import Job, enqueue
from trading.jobs.runner import JobRunner


def _runner(
    sf: sessionmaker[Session], clock: ManualClock, handlers: dict[str, object]
) -> JobRunner:
    return JobRunner(sf, handlers, clock=clock, worker_id="w1")  # type: ignore[arg-type]


def _add(sf: sessionmaker[Session], clock: ManualClock, kind: str, key: str = "k") -> None:
    with sf.begin() as s:
        enqueue(s, kind=kind, payload={"x": 1}, scheduled_at=clock.now(), dedupe_key=key)


def _state(db: Session) -> tuple[str, int, str | None]:
    db.rollback()
    row = db.execute(text("select state, attempts, last_error from jobs")).one()
    return row[0], row[1], row[2]


def test_run_once_returns_false_when_idle(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    assert _runner(session_factory, clock, {}).run_once() is False


def test_runner_runs_handler_and_completes(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    seen: list[Job] = []
    _add(session_factory, clock, "ping")
    assert _runner(session_factory, clock, {"ping": seen.append}).run_once() is True
    assert [j.payload for j in seen] == [{"x": 1}]
    assert _state(db)[0] == "SUCCEEDED"


def test_runner_unknown_kind_fails(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    _add(session_factory, clock, "mystery")
    _runner(session_factory, clock, {}).run_once()
    state, attempts, error = _state(db)
    assert (state, attempts) == ("QUEUED", 1)
    assert error is not None
    assert "no handler for kind" in error


def test_runner_handler_exception_records_error_and_backs_off(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    def boom(job: Job) -> None:
        raise RuntimeError("kaboom")

    _add(session_factory, clock, "ping")
    runner = _runner(session_factory, clock, {"ping": boom})
    assert runner.run_once() is True
    state, _attempts, error = _state(db)
    assert state == "QUEUED"
    assert error is not None
    assert "kaboom" in error
    assert runner.run_once() is False  # backed off: not yet due
    clock.advance(timedelta(seconds=3))
    assert runner.run_once() is True


def test_runner_redacts_secrets_from_persisted_error(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    def leak(job: Job) -> None:
        raise RuntimeError("upstream said Authorization: Bearer abc.def key trk_s3cr3t")

    _add(session_factory, clock, "ping")
    _runner(session_factory, clock, {"ping": leak}).run_once()
    error = _state(db)[2]
    assert error is not None
    assert "abc.def" not in error
    assert "trk_s3cr3t" not in error
    assert "RuntimeError" in error


def test_runner_counts_job_outcomes(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    from prometheus_client import REGISTRY

    def sample(kind: str, outcome: str) -> float:
        labels = {"kind": kind, "outcome": outcome}
        return REGISTRY.get_sample_value("trading_jobs_processed_total", labels) or 0.0

    ok_before, bad_before = sample("count-ok", "succeeded"), sample("count-bad", "failed")

    def boom(job: Job) -> None:
        raise RuntimeError("x")

    _add(session_factory, clock, "count-ok", key="a")
    _add(session_factory, clock, "count-bad", key="b")
    runner = _runner(session_factory, clock, {"count-ok": lambda j: None, "count-bad": boom})
    runner.run_once()
    runner.run_once()
    assert sample("count-ok", "succeeded") == ok_before + 1
    assert sample("count-bad", "failed") == bad_before + 1


def test_runner_lets_lease_loss_and_database_errors_escape(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    """Review I6: a fenced-out or database-less worker must stop, not record a job failure."""
    import pytest
    from sqlalchemy.exc import OperationalError

    from trading.jobs.lease import LeaseLost

    def lost(job: Job) -> None:
        raise LeaseLost("taken over")

    def db_down(job: Job) -> None:
        raise OperationalError("select 1", {}, Exception("connection refused"))

    for kind, handler, exc in (("lost", lost, LeaseLost), ("down", db_down, OperationalError)):
        _add(session_factory, clock, kind, key=kind)
        with pytest.raises(exc):
            _runner(session_factory, clock, {kind: handler}).run_once()
    states = dict(db.execute(text("select kind, state from jobs")).all())
    assert states == {
        "lost": "RUNNING",
        "down": "RUNNING",
    }  # left for lease-expiry reclaim, not failed
