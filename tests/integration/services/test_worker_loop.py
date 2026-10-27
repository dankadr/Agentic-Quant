from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.services.helpers import (
    FlakyFactory,
    job_states,
    lease_holder,
    start_worker,
    wait_until,
)
from trading.core.clock import SystemClock
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease
from trading.jobs.queue import enqueue
from trading.services.handlers import WorkerContext


def _enqueue_heartbeat(sf: sessionmaker[Session], key: str = "hb") -> None:
    with sf.begin() as s:
        enqueue(s, kind="heartbeat", payload={}, scheduled_at=SystemClock().now(), dedupe_key=key)


def test_worker_processes_job_and_releases_lease_on_stop(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    _enqueue_heartbeat(session_factory)
    worker = start_worker(session_factory)
    assert wait_until(lambda: job_states(session_factory) == ["SUCCEEDED"])
    assert lease_holder(session_factory, EXECUTOR_LEASE) == "w1"
    assert worker.stop_and_join() == 0
    assert lease_holder(session_factory, EXECUTOR_LEASE) is None


def test_worker_exits_when_lease_renew_fails(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    worker = start_worker(session_factory)
    assert wait_until(lambda: lease_holder(session_factory, EXECUTOR_LEASE) == "w1")
    with session_factory.begin() as s:  # simulate another process taking over
        s.execute(
            text(
                "update leases set holder = 'thief', fencing_token = fencing_token + 1,"
                " expires_at = now() + interval '1 hour'"
            )
        )
    worker.thread.join(10)
    assert not worker.thread.is_alive()
    assert worker.result["code"] == 3


def test_only_one_worker_holds_executor_lease(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    w1 = start_worker(session_factory, "w1")
    w2 = start_worker(session_factory, "w2")
    assert wait_until(lambda: len(w1.contexts) + len(w2.contexts) >= 1)
    _enqueue_heartbeat(session_factory)
    assert wait_until(lambda: job_states(session_factory) == ["SUCCEEDED"])
    assert len(w1.contexts) + len(w2.contexts) == 1  # the loser never got a context
    holder = lease_holder(session_factory, EXECUTOR_LEASE)
    assert holder == ("w1" if w1.contexts else "w2")
    w1.stop_and_join()
    w2.stop_and_join()


def test_standby_worker_takes_over_after_leader_stops(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    w1 = start_worker(session_factory, "w1")
    assert wait_until(lambda: lease_holder(session_factory, EXECUTOR_LEASE) == "w1")
    w2 = start_worker(session_factory, "w2")
    assert w1.stop_and_join() == 0
    assert wait_until(lambda: lease_holder(session_factory, EXECUTOR_LEASE) == "w2")
    assert w2.stop_and_join() == 0


def test_worker_stopped_while_waiting_for_lease_returns_zero(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        acquire_lease(
            s, name=EXECUTOR_LEASE, holder="other", now=SystemClock().now(), ttl=timedelta(hours=1)
        )
    worker = start_worker(session_factory, "w1")
    assert worker.stop_and_join() == 0
    assert worker.contexts == []


def test_worker_exits_nonzero_when_database_fails(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    flaky = FlakyFactory(session_factory)
    worker = start_worker(flaky)
    assert wait_until(lambda: lease_holder(session_factory, EXECUTOR_LEASE) == "w1")
    flaky.armed.set()
    worker.thread.join(10)
    assert not worker.thread.is_alive()
    assert worker.result["code"] == 3


def test_failing_startup_hook_aborts_worker_and_releases_lease(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    def boom(ctx: WorkerContext) -> None:
        raise RuntimeError("reconciliation failed")

    _enqueue_heartbeat(session_factory)
    worker = start_worker(session_factory, startup_hooks=[boom])
    worker.thread.join(10)
    assert worker.result["code"] == 1
    assert job_states(session_factory) == ["QUEUED"]  # no job processed before startup succeeded
    assert lease_holder(session_factory, EXECUTOR_LEASE) is None


def test_startup_hooks_run_before_any_job(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seen: list[list[str]] = []

    def hook(ctx: WorkerContext) -> None:
        seen.append(job_states(session_factory))

    _enqueue_heartbeat(session_factory)
    worker = start_worker(session_factory, startup_hooks=[hook])
    assert wait_until(lambda: job_states(session_factory) == ["SUCCEEDED"])
    worker.stop_and_join()
    assert seen == [["QUEUED"]]


@pytest.mark.parametrize("role", ["worker", "scheduler"])
def test_placeholder_roles_are_known(role: str) -> None:
    from trading.services.health import lease_name_for

    assert lease_name_for(role) in {"executor", "scheduler"}


@pytest.mark.parametrize("which", ["lease_lost", "database"])
def test_worker_exits_nonzero_when_a_handler_hits_lease_loss_or_a_database_error(
    db: Session, session_factory: sessionmaker[Session], which: str
) -> None:
    from sqlalchemy.exc import OperationalError

    from trading.jobs.lease import LeaseLost

    def boom(job):  # type: ignore[no-untyped-def]
        raise (
            LeaseLost("taken over")
            if which == "lease_lost"
            else OperationalError("x", {}, Exception("down"))
        )

    _enqueue_heartbeat(session_factory)
    worker = start_worker(session_factory, handlers_factory=lambda ctx: {"heartbeat": boom})
    worker.thread.join(10)
    assert not worker.thread.is_alive()
    assert worker.result["code"] == 3


def _apply_config_change(sf: sessionmaker[Session]) -> None:
    """An approved agent-config edit applied just now (after any worker started)."""
    with sf.begin() as s:
        s.execute(
            text(
                "insert into agent_config_proposals (agent_name, changes_json, base_json,"
                " rationale, status, source, proposed_by, created_at, applied_at) values"
                " ('macro', '{\"run_every_minutes\": 15}', '{\"run_every_minutes\": 30}', 'r',"
                " 'APPLIED', 'OPERATOR', 'op', :t, :t)"
            ),
            {"t": SystemClock().now() + timedelta(seconds=1)},
        )


def test_worker_exits_for_reload_when_agent_config_was_applied(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    worker = start_worker(session_factory, config_check_interval=0.1)
    assert wait_until(lambda: lease_holder(session_factory, EXECUTOR_LEASE) == "w1")
    _apply_config_change(session_factory)
    worker.thread.join(10)
    assert not worker.thread.is_alive()
    assert worker.result["code"] == 4
    assert lease_holder(session_factory, EXECUTOR_LEASE) is None  # released for the restart


def test_reload_exit_waits_for_the_running_job(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    import threading

    started = threading.Event()

    def slow_handlers(ctx: WorkerContext) -> dict:  # type: ignore[type-arg]
        def heartbeat(job: object) -> None:
            started.set()
            _apply_config_change(session_factory)  # applied while the job is running
            started.wait(0.5)
            threading.Event().wait(0.8)

        return {"heartbeat": heartbeat}

    _enqueue_heartbeat(session_factory)
    worker = start_worker(
        session_factory, handlers_factory=slow_handlers, config_check_interval=0.1
    )
    worker.thread.join(15)
    assert worker.result["code"] == 4
    assert job_states(session_factory) == ["SUCCEEDED"]  # the job finished before the exit


def test_worker_started_with_an_older_config_version_reloads_at_once(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    # An edit landed after this process loaded its config but before it got the lease.
    with session_factory.begin() as s:
        s.execute(
            text(
                "insert into agent_config_proposals (agent_name, changes_json, base_json,"
                " rationale, status, source, proposed_by, created_at, applied_at) values"
                " ('macro', '{\"run_every_minutes\": 15}', '{\"run_every_minutes\": 30}', 'r',"
                " 'APPLIED', 'OPERATOR', 'op', now(), now() - interval '1 minute')"
            )
        )
    worker = start_worker(session_factory, config_version=None, config_check_interval=0.1)
    worker.thread.join(10)
    assert not worker.thread.is_alive()
    assert worker.result["code"] == 4
