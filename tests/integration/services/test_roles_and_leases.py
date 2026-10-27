"""Executor lease survives a slow job; the research worker runs research jobs and nothing else."""

import threading
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.services.helpers import wait_until
from trading.core.clock import SystemClock
from trading.core.config import Settings, load_config
from trading.jobs.kinds import EXECUTION_KINDS, OPERATIONS_KINDS, RESEARCH_KINDS
from trading.jobs.lease import EXECUTOR_LEASE, current_lease
from trading.jobs.queue import Job, enqueue
from trading.services.exit_codes import EXIT_OK
from trading.services.handlers import WorkerContext
from trading.services.research import run_research_worker
from trading.services.worker import run_worker

TTL = timedelta(seconds=1.5)


def _enqueue(session_factory: sessionmaker[Session], kind: str, key: str) -> None:
    with session_factory.begin() as s:
        enqueue(s, kind=kind, payload={}, scheduled_at=SystemClock().now(), dedupe_key=key)


def _state(db: Session, kind: str) -> str:
    db.rollback()
    return db.execute(text("select state from jobs where kind = :k"), {"k": kind}).scalar_one()


def test_slow_job_does_not_cost_the_executor_its_lease(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    """Regression: renewal used to run only between jobs, so a 4 s job outlived a 1.5 s lease."""
    clock = SystemClock()
    started = threading.Event()
    lease_seen_mid_job: list[bool] = []

    def slow(job: Job) -> None:
        started.set()
        time.sleep(4.0)  # more than two full TTLs
        with session_factory.begin() as s:
            lease_seen_mid_job.append(current_lease(s, EXECUTOR_LEASE, now=clock.now()) is not None)

    def ctx_factory(lease) -> WorkerContext:  # type: ignore[no-untyped-def]
        return WorkerContext(
            settings=Settings(),
            config=load_config(Path("configs")),
            session_factory=session_factory,
            clock=clock,
            lease=lease,
        )

    _enqueue(session_factory, "slow", "slow-1")
    stop = threading.Event()
    result: dict[str, int] = {}
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "code",
            run_worker(
                session_factory=session_factory,
                clock=clock,
                worker_id="w-slow",
                ctx_factory=ctx_factory,
                stop=stop,
                lease_ttl=TTL,
                poll=0.05,
                handlers_factory=lambda _ctx: {"slow": slow},
                kinds={"slow"},
            ),
        )
    )
    worker.start()
    assert started.wait(10)
    assert wait_until(lambda: _state(db, "slow") == "SUCCEEDED", timeout=20)
    stop.set()
    worker.join(10)

    assert lease_seen_mid_job == [True]
    assert result["code"] == EXIT_OK


def test_research_worker_runs_research_jobs_and_ignores_execution_kinds(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    clock = SystemClock()
    ran: list[str] = []

    def macro(job: Job) -> None:
        ran.append(job.kind)

    _enqueue(session_factory, "agent_macro", "m-1")
    _enqueue(session_factory, "paper_tick", "t-1")  # must stay queued for the executor
    stop = threading.Event()
    worker = threading.Thread(
        target=run_research_worker,
        kwargs={
            "session_factory": session_factory,
            "clock": clock,
            "worker_id": "research-1",
            "handlers": {"agent_macro": macro, "paper_tick": macro},
            "stop": stop,
            "poll": 0.05,
        },
    )
    worker.start()
    assert wait_until(lambda: ran == ["agent_macro"], timeout=10)
    stop.set()
    worker.join(10)

    db.rollback()
    assert _state(db, "agent_macro") == "SUCCEEDED"
    assert _state(db, "paper_tick") == "QUEUED"
    assert not EXECUTION_KINDS & RESEARCH_KINDS
    assert not OPERATIONS_KINDS & RESEARCH_KINDS
