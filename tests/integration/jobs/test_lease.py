from datetime import timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.jobs.lease import (
    EXECUTOR_LEASE,
    LeaseLost,
    acquire_lease,
    assert_fence,
    current_lease,
    release_lease,
    renew_lease,
)

TTL = timedelta(seconds=30)


def test_second_holder_blocked_while_valid(db: Session, clock: ManualClock) -> None:
    first = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert first is not None
    assert first.fencing_token == 1
    assert acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL) is None


def test_takeover_after_expiry_increments_token(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(TTL + timedelta(seconds=1))
    l2 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL)
    assert l2 is not None
    assert l2.fencing_token == l1.fencing_token + 1


def test_stale_holder_renew_fails(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(TTL + timedelta(seconds=1))
    assert acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL)
    assert renew_lease(db, l1, now=clock.now(), ttl=TTL) is None


def test_stale_holder_fence_fails(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    assert_fence(db, l1, now=clock.now())
    clock.advance(TTL + timedelta(seconds=1))
    assert acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL)
    with pytest.raises(LeaseLost):
        assert_fence(db, l1, now=clock.now())


def test_fence_fails_when_lease_expired_even_if_untouched(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(TTL)  # expires_at == now counts as expired
    with pytest.raises(LeaseLost):
        assert_fence(db, l1, now=clock.now())
    assert renew_lease(db, l1, now=clock.now(), ttl=TTL) is None


def test_fence_fails_when_no_lease_row(db: Session, clock: ManualClock) -> None:
    from trading.jobs.lease import Lease

    ghost = Lease(
        name="nope", holder="w1", fencing_token=1, expires_at=clock.now() + timedelta(hours=1)
    )
    with pytest.raises(LeaseLost):
        assert_fence(db, ghost, now=clock.now())


def test_renew_extends_expiry(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(timedelta(seconds=20))
    l2 = renew_lease(db, l1, now=clock.now(), ttl=TTL)
    assert l2 is not None
    assert l2.fencing_token == l1.fencing_token
    assert l2.expires_at == clock.now() + TTL


def test_release_allows_immediate_acquire(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    release_lease(db, l1)
    l2 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL)
    assert l2 is not None
    assert l2.fencing_token == l1.fencing_token + 1


def test_release_by_stale_holder_is_ignored(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(TTL + timedelta(seconds=1))
    l2 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w2", now=clock.now(), ttl=TTL)
    assert l2 is not None
    release_lease(db, l1)  # must not release w2's lease
    assert acquire_lease(db, name=EXECUTOR_LEASE, holder="w3", now=clock.now(), ttl=TTL) is None


def test_same_holder_reacquire_while_valid_keeps_token(db: Session, clock: ManualClock) -> None:
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    l2 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    assert l2 is not None
    assert l2.fencing_token == l1.fencing_token


def test_same_holder_reacquire_after_expiry_bumps_token(db: Session, clock: ManualClock) -> None:
    """A restarted process reusing its holder id must fence out its own zombie."""
    l1 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l1 is not None
    clock.advance(TTL + timedelta(seconds=1))
    l2 = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    assert l2 is not None
    assert l2.fencing_token == l1.fencing_token + 1
    with pytest.raises(LeaseLost):
        assert_fence(db, l1, now=clock.now())


def test_current_lease_only_when_unexpired(db: Session, clock: ManualClock) -> None:
    assert current_lease(db, EXECUTOR_LEASE, now=clock.now()) is None
    acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=TTL)
    held = current_lease(db, EXECUTOR_LEASE, now=clock.now())
    assert held is not None
    assert held.holder == "w1"
    clock.advance(TTL)
    assert current_lease(db, EXECUTOR_LEASE, now=clock.now()) is None


def test_concurrent_acquire_single_winner(
    db: Session, session_factory: sessionmaker[Session], clock: ManualClock
) -> None:
    results = []
    for holder in ("w1", "w2", "w3"):
        with session_factory.begin() as s:
            results.append(
                acquire_lease(s, name=EXECUTOR_LEASE, holder=holder, now=clock.now(), ttl=TTL)
            )
    assert [r is not None for r in results] == [True, False, False]
