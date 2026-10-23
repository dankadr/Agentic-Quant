from datetime import timedelta

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from trading.cli import app
from trading.core.clock import ManualClock
from trading.core.config import Settings
from trading.jobs.lease import EXECUTOR_LEASE, SCHEDULER_LEASE, acquire_lease
from trading.services.health import check_health


def test_check_health_true_only_for_own_unexpired_lease(db: Session, clock: ManualClock) -> None:
    settings = Settings(worker_id="me")
    assert check_health("worker", db, settings, clock) is False
    acquire_lease(
        db, name=EXECUTOR_LEASE, holder="someone-else", now=clock.now(), ttl=timedelta(hours=1)
    )
    assert check_health("worker", db, settings, clock) is False
    db.execute(__import__("sqlalchemy").text("delete from leases"))
    acquire_lease(db, name=EXECUTOR_LEASE, holder="me", now=clock.now(), ttl=timedelta(seconds=30))
    assert check_health("worker", db, settings, clock) is True
    assert check_health("scheduler", db, settings, clock) is False
    acquire_lease(db, name=SCHEDULER_LEASE, holder="me", now=clock.now(), ttl=timedelta(seconds=30))
    assert check_health("scheduler", db, settings, clock) is True
    clock.advance(timedelta(seconds=31))
    assert check_health("worker", db, settings, clock) is False


def test_healthcheck_cli_exit_code_reflects_lease(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch, clock: ManualClock
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    monkeypatch.setenv("TRADING_WORKER_ID", "cli-me")
    runner = CliRunner()
    assert runner.invoke(app, ["healthcheck", "worker"]).exit_code == 1
    from trading.core.clock import SystemClock

    acquire_lease(
        db, name=EXECUTOR_LEASE, holder="cli-me", now=SystemClock().now(), ttl=timedelta(hours=1)
    )
    db.commit()
    assert runner.invoke(app, ["healthcheck", "worker"]).exit_code == 0
    assert runner.invoke(app, ["healthcheck", "bogus"]).exit_code != 0


def test_health_matches_a_per_process_holder_of_this_worker(
    db: Session, clock: ManualClock
) -> None:
    """Review I2: holders are unique per process ('<worker_id>:<pid>:<nonce>'), health matches the prefix."""
    from trading.services.health import new_holder_id

    settings = Settings(worker_id="me")
    holder = new_holder_id("me")
    assert holder.startswith("me:")
    assert new_holder_id("me") != holder
    acquire_lease(
        db, name=EXECUTOR_LEASE, holder=holder, now=clock.now(), ttl=timedelta(seconds=30)
    )
    assert check_health("worker", db, settings, clock) is True
    assert (
        check_health("worker", db, Settings(worker_id="m"), clock) is False
    )  # not a mere string prefix
    assert check_health("worker", db, Settings(worker_id="me:1"), clock) is False


def test_two_processes_sharing_a_worker_id_cannot_both_hold_the_lease(
    db: Session, clock: ManualClock
) -> None:
    from trading.services.health import new_holder_id

    first = acquire_lease(
        db,
        name=EXECUTOR_LEASE,
        holder=new_holder_id("host"),
        now=clock.now(),
        ttl=timedelta(seconds=30),
    )
    second = acquire_lease(
        db,
        name=EXECUTOR_LEASE,
        holder=new_holder_id("host"),
        now=clock.now(),
        ttl=timedelta(seconds=30),
    )
    assert first is not None
    assert second is None
