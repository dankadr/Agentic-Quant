"""Shared helpers for threaded service-loop tests (real clock, short lease TTLs)."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import SystemClock
from trading.core.config import Settings, load_config
from trading.jobs.lease import Lease
from trading.services.handlers import WorkerContext
from trading.services.worker import run_worker

TTL = timedelta(seconds=1.5)


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class FlakyFactory:
    """Delegates to a real session factory until armed, then raises OperationalError."""

    def __init__(self, real: sessionmaker[Session]) -> None:
        self._real = real
        self.armed = threading.Event()

    def _check(self) -> None:
        if self.armed.is_set():
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    def begin(self) -> Any:
        self._check()
        return self._real.begin()

    def __call__(self) -> Session:
        self._check()
        return self._real()


@dataclass
class WorkerHandle:
    thread: threading.Thread
    stop: threading.Event
    result: dict[str, int] = field(default_factory=dict)
    contexts: list[WorkerContext] = field(default_factory=list)

    def stop_and_join(self, timeout: float = 10.0) -> int:
        self.stop.set()
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "worker did not stop"
        return self.result["code"]


def start_worker(
    sf: Any,
    worker_id: str = "w1",
    startup_hooks: Any = (),
    lease_ttl: timedelta = TTL,
) -> WorkerHandle:
    stop = threading.Event()
    handle = WorkerHandle(thread=threading.Thread(daemon=True), stop=stop)
    settings = Settings()
    config = load_config(Path("configs"))
    clock = SystemClock()

    def ctx_factory(lease: Lease) -> WorkerContext:
        ctx = WorkerContext(
            settings=settings, config=config, session_factory=sf, clock=clock, lease=lease
        )
        handle.contexts.append(ctx)
        return ctx

    def target() -> None:
        handle.result["code"] = run_worker(
            session_factory=sf,
            clock=clock,
            worker_id=worker_id,
            ctx_factory=ctx_factory,
            stop=stop,
            lease_ttl=lease_ttl,
            poll=0.05,
            acquire_retry=0.1,
            startup_hooks=startup_hooks,
        )

    handle.thread = threading.Thread(target=target, daemon=True)
    handle.thread.start()
    return handle


def job_states(sf: sessionmaker[Session]) -> list[str]:
    with sf() as s:
        return list(s.execute(text("select state from jobs order by created_at")).scalars())


def lease_holder(sf: sessionmaker[Session], name: str) -> str | None:
    with sf() as s:
        return s.execute(
            text("select holder from leases where name = :n and expires_at > now()"), {"n": name}
        ).scalar_one_or_none()
