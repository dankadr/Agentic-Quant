"""Container health checks: a service is healthy while it holds its own, unexpired lease."""

import os
from uuid import uuid4

from sqlalchemy.orm import Session

from trading.core.clock import Clock
from trading.core.config import Settings
from trading.jobs.lease import EXECUTOR_LEASE, SCHEDULER_LEASE, current_lease


def lease_name_for(role: str) -> str:
    try:
        return {"worker": EXECUTOR_LEASE, "scheduler": SCHEDULER_LEASE}[role]
    except KeyError:
        raise ValueError(f"unknown service role {role!r}") from None


def new_holder_id(worker_id: str) -> str:
    """A lease holder id unique to THIS process: ``<worker_id>:<pid>:<nonce>``.

    Two processes sharing a worker id (a second ``trading worker`` on the same host) must not
    be able to hold the same lease, or fencing could not tell them apart.
    """
    return f"{worker_id}:{os.getpid()}:{uuid4().hex[:8]}"


def check_health(role: str, session: Session, settings: Settings, clock: Clock) -> bool:
    lease = current_lease(session, lease_name_for(role), now=clock.now())
    if lease is None:
        return False
    return lease.holder == settings.worker_id or lease.holder.startswith(f"{settings.worker_id}:")
