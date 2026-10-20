from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.jobs.queue import JobState, claim, complete, enqueue, fail

LEASE = timedelta(minutes=1)


def _enqueue(
    sf: sessionmaker[Session],
    clock: ManualClock,
    key: str,
    *,
    kind: str = "k",
    at: datetime | None = None,
    max_attempts: int = 5,
) -> UUID:
    with sf.begin() as s:
        return enqueue(
            s,
            kind=kind,
            payload={"n": 1},
            scheduled_at=at or clock.now(),
            dedupe_key=key,
            max_attempts=max_attempts,
        )


def _row(db: Session, job_id: UUID) -> dict[str, object]:
    db.rollback()
    row = db.execute(
        text(
            "select state, attempts, locked_by, scheduled_at, last_error, lease_until"
            " from jobs where id = :id"
        ),
        {"id": job_id},
    ).one()
    return dict(row._mapping)


def test_enqueue_dedupes(db: Session, session_factory: sessionmaker[Session], clock: ManualClock):
    a = _enqueue(session_factory, clock, "k")
    b = _enqueue(session_factory, clock, "k")
    assert a == b
    assert db.execute(text("select count(*) from jobs")).scalar_one() == 1


def test_not_claimed_before_scheduled(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    _enqueue(session_factory, clock, "k", at=clock.now() + timedelta(seconds=10))
    with session_factory.begin() as s:
        assert claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE) is None
    clock.advance(timedelta(seconds=10))
    with session_factory.begin() as s:
        job = claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE)
    assert job is not None
    assert job.payload == {"n": 1}
    assert job.attempts == 1


def test_concurrent_claims_get_distinct_jobs(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    first = _enqueue(session_factory, clock, "a")
    second = _enqueue(session_factory, clock, "b", at=clock.now() + timedelta(seconds=1))
    clock.advance(timedelta(seconds=5))
    s1, s2, s3 = session_factory(), session_factory(), session_factory()
    try:
        j1 = claim(s1, worker_id="w1", now=clock.now(), lease_for=LEASE)  # uncommitted
        j2 = claim(s2, worker_id="w2", now=clock.now(), lease_for=LEASE)
        j3 = claim(s3, worker_id="w3", now=clock.now(), lease_for=LEASE)
        assert j1 is not None
        assert j2 is not None
        assert j3 is None
        assert {j1.id, j2.id} == {first, second}
    finally:
        for s in (s1, s2, s3):
            s.rollback()
            s.close()


def test_claim_orders_by_scheduled_at(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    late = _enqueue(session_factory, clock, "late", at=clock.now() - timedelta(seconds=1))
    early = _enqueue(session_factory, clock, "early", at=clock.now() - timedelta(seconds=9))
    with session_factory.begin() as s:
        job = claim(s, worker_id="w", now=clock.now(), lease_for=LEASE)
    assert job is not None
    assert job.id == early
    assert late != early


def test_kinds_filter(db: Session, session_factory: sessionmaker[Session], clock: ManualClock):
    _enqueue(session_factory, clock, "a", kind="alpha")
    with session_factory.begin() as s:
        assert claim(s, worker_id="w", now=clock.now(), lease_for=LEASE, kinds=["beta"]) is None
        job = claim(s, worker_id="w", now=clock.now(), lease_for=LEASE, kinds=["alpha", "beta"])
    assert job is not None
    assert job.kind == "alpha"


def test_expired_lease_is_reclaimed(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k")
    with session_factory.begin() as s:
        assert claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE) is not None
    with session_factory.begin() as s:  # lease still valid: nothing to claim
        assert claim(s, worker_id="w2", now=clock.now(), lease_for=LEASE) is None
    clock.advance(timedelta(minutes=2))
    with session_factory.begin() as s:
        job = claim(s, worker_id="w2", now=clock.now(), lease_for=LEASE)
    assert job is not None
    assert job.id == jid
    assert job.attempts == 2
    assert _row(db, jid)["locked_by"] == "w2"


def test_expired_lease_at_max_attempts_goes_dead(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k", max_attempts=1)
    with session_factory.begin() as s:
        assert claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE) is not None
    clock.advance(timedelta(minutes=2))
    with session_factory.begin() as s:
        assert claim(s, worker_id="w2", now=clock.now(), lease_for=LEASE) is None
    assert _row(db, jid)["state"] == "DEAD"


def test_complete_marks_succeeded(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k")
    with session_factory.begin() as s:
        claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE)
    with session_factory.begin() as s:
        complete(s, jid, worker_id="w1", attempts=1, now=clock.now())
    row = _row(db, jid)
    assert row["state"] == "SUCCEEDED"
    assert row["locked_by"] is None


def test_fail_backoff_then_dead(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k", max_attempts=2)
    with session_factory.begin() as s:
        claim(s, worker_id="w", now=clock.now(), lease_for=LEASE)
    with session_factory.begin() as s:
        assert (
            fail(s, jid, worker_id="w", attempts=1, error="boom", now=clock.now())
            is JobState.QUEUED
        )
    row = _row(db, jid)
    assert row["scheduled_at"] == clock.now() + timedelta(seconds=2)
    assert row["last_error"] == "boom"
    clock.advance(timedelta(seconds=2))
    with session_factory.begin() as s:
        claim(s, worker_id="w", now=clock.now(), lease_for=LEASE)
    with session_factory.begin() as s:
        assert (
            fail(s, jid, worker_id="w", attempts=2, error="x" * 5000, now=clock.now())
            is JobState.DEAD
        )
    row = _row(db, jid)
    assert row["state"] == "DEAD"
    assert len(str(row["last_error"])) == 2000


def test_stale_worker_cannot_complete_reclaimed_job(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k")
    with session_factory.begin() as s:
        claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE)
    clock.advance(timedelta(minutes=2))
    with session_factory.begin() as s:
        claim(s, worker_id="w2", now=clock.now(), lease_for=LEASE)
    with session_factory.begin() as s:  # w1 wakes up late
        assert complete(s, jid, worker_id="w1", attempts=1, now=clock.now()) is False
    row = _row(db, jid)
    assert (row["state"], row["locked_by"], row["attempts"]) == ("RUNNING", "w2", 2)
    with session_factory.begin() as s:
        assert complete(s, jid, worker_id="w2", attempts=2, now=clock.now()) is True


def test_stale_worker_cannot_fail_reclaimed_job(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
):
    jid = _enqueue(session_factory, clock, "k")
    with session_factory.begin() as s:
        claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE)
    clock.advance(timedelta(minutes=2))
    with session_factory.begin() as s:
        claim(s, worker_id="w2", now=clock.now(), lease_for=LEASE)
    with session_factory.begin() as s:
        assert fail(s, jid, worker_id="w1", attempts=1, error="late", now=clock.now()) is None
    row = _row(db, jid)
    assert (row["state"], row["locked_by"], row["last_error"]) == ("RUNNING", "w2", None)
