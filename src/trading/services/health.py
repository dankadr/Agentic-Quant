"""Container health checks: a service is healthy while it holds its own, unexpired lease."""

from sqlalchemy.orm import Session

from trading.core.clock import Clock
from trading.core.config import Settings
from trading.jobs.lease import EXECUTOR_LEASE, SCHEDULER_LEASE, current_lease


def lease_name_for(role: str) -> str:
    try:
        return {"worker": EXECUTOR_LEASE, "scheduler": SCHEDULER_LEASE}[role]
    except KeyError:
        raise ValueError(f"unknown service role {role!r}") from None


def check_health(role: str, session: Session, settings: Settings, clock: Clock) -> bool:
    lease = current_lease(session, lease_name_for(role), now=clock.now())
    return lease is not None and lease.holder == settings.worker_id
