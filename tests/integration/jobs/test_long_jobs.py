"""A long-running handler keeps its job lease, and role-restricted runners never see other kinds."""

import time
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock, SystemClock
from trading.jobs.kinds import EXECUTION_KINDS, OPERATIONS_KINDS, RESEARCH_KINDS
from trading.jobs.queue import Job, claim, complete, enqueue
from trading.jobs.runner import JobRunner

LEASE = timedelta(seconds=0.6)


def _add(sf: sessionmaker[Session], kind: str, key: str) -> None:
    with sf.begin() as s:
        enqueue(s, kind=kind, payload={}, scheduled_at=SystemClock().now(), dedupe_key=key)


def test_handler_longer_than_its_lease_is_not_reclaimed(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """Without renewal, a second claimant could take the job after LEASE and run it twice."""
    clock = SystemClock()
    takeover: list[bool] = []

    def slow(job: Job) -> None:
        time.sleep(LEASE.total_seconds() * 3)
        with session_factory.begin() as other:
            takeover.append(
                claim(other, worker_id="rival", now=clock.now(), lease_for=LEASE) is not None
            )

    _add(session_factory, "slow", "slow-1")
    JobRunner(
        session_factory, {"slow": slow}, clock=clock, worker_id="w1", lease_for=LEASE
    ).run_once()

    assert takeover == [False]
    db.rollback()
    state, attempts = db.execute(text("select state, attempts from jobs")).one()
    assert (state, attempts) == ("SUCCEEDED", 1)


def test_reclaimed_claim_result_is_dropped(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """A claim whose lease lapsed (no renewal) is reclaimed; its late completion commits nothing."""
    clock = ManualClock(SystemClock().now())
    with session_factory.begin() as s:
        enqueue(s, kind="ping", payload={}, scheduled_at=clock.now(), dedupe_key="ping-1")
    with session_factory.begin() as s:
        first = claim(s, worker_id="w1", now=clock.now(), lease_for=LEASE)
    assert first is not None
    later = clock.now() + LEASE + timedelta(seconds=1)
    with session_factory.begin() as s:
        second = claim(s, worker_id="w2", now=later, lease_for=LEASE)
    assert second is not None and second.attempts == 2

    with session_factory.begin() as s:
        assert complete(s, first.id, worker_id="w1", attempts=first.attempts, now=later) is False


def test_each_role_claims_only_its_own_kinds(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    clock = SystemClock()
    for kind, key in [
        ("paper_tick", "t"),
        ("heartbeat", "h"),
        ("agent_macro", "m"),
    ]:
        _add(session_factory, kind, key)

    with session_factory.begin() as s:
        executor = claim(
            s,
            worker_id="exec",
            now=clock.now(),
            lease_for=LEASE,
            kinds=EXECUTION_KINDS | OPERATIONS_KINDS,
        )
    with session_factory.begin() as s:
        research = claim(s, worker_id="res", now=clock.now(), lease_for=LEASE, kinds=RESEARCH_KINDS)
    with session_factory.begin() as s:
        leftover = claim(
            s,
            worker_id="exec",
            now=clock.now(),
            lease_for=LEASE,
            kinds=EXECUTION_KINDS | OPERATIONS_KINDS,
        )

    assert executor is not None and executor.kind in EXECUTION_KINDS | OPERATIONS_KINDS
    assert research is not None and research.kind == "agent_macro"
    assert leftover is not None and leftover.kind in EXECUTION_KINDS | OPERATIONS_KINDS
    assert leftover.kind != executor.kind
