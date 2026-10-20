"""Job handlers run by the trading worker."""

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings
from trading.jobs.lease import EXECUTOR_LEASE, Lease
from trading.jobs.queue import Job
from trading.jobs.runner import Handler
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD

_log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    config: AppConfig
    session_factory: sessionmaker[Session]
    clock: Clock
    lease: Lease


def build_handlers(ctx: WorkerContext) -> dict[str, Handler]:
    def heartbeat(job: Job) -> None:
        LEASE_HELD.labels(EXECUTOR_LEASE).set(1)
        _log.info("heartbeat", job_id=str(job.id), fencing_token=ctx.lease.fencing_token)

    return {"heartbeat": heartbeat}
