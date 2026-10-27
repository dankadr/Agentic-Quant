"""The research worker: model-driven agent jobs, kept out of the execution path.

It holds no executor lease, builds no broker adapter, and claims only ``RESEARCH_KINDS``. A stalled
model call therefore delays research output, never a tick, an exit, or an operator command.
"""

import threading
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.jobs import consensus_job, evaluator_job, fundamental_job, macro_job
from trading.agents.macro import JsonLlm
from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings
from trading.data.calendar import MarketCalendar
from trading.data.news import NewsSource
from trading.jobs.kinds import RESEARCH_KINDS
from trading.jobs.queue import Job
from trading.jobs.runner import Handler, JobRunner
from trading.monitoring.logging import get_logger
from trading.services.exit_codes import EXIT_LEASE_LOST, EXIT_OK

_log = get_logger(__name__)


@dataclass(frozen=True)
class ResearchContext:
    settings: Settings
    config: AppConfig
    session_factory: sessionmaker[Session]
    clock: Clock
    calendar: MarketCalendar
    provider_name: str
    llm: JsonLlm | None = None
    news: NewsSource | None = None


def build_research_handlers(ctx: ResearchContext) -> dict[str, Handler]:
    def agent_fundamental(job: Job) -> None:
        fundamental_job(ctx.session_factory, ctx.llm, ctx.news, config=ctx.config, clock=ctx.clock)

    def agent_consensus(job: Job) -> None:
        consensus_job(ctx.session_factory, config=ctx.config, clock=ctx.clock, payload=job.payload)

    def agent_macro(job: Job) -> None:
        macro_job(
            ctx.session_factory,
            ctx.llm,
            config=ctx.config,
            calendar=ctx.calendar,
            provider_name=ctx.provider_name,
            clock=ctx.clock,
            payload=job.payload,
        )

    def agent_evaluator(job: Job) -> None:
        evaluator_job(
            ctx.session_factory,
            ctx.llm,
            config=ctx.config,
            provider_name=ctx.provider_name,
            clock=ctx.clock,
            payload=job.payload,
        )

    return {
        "agent_fundamental": agent_fundamental,
        "agent_consensus": agent_consensus,
        "agent_macro": agent_macro,
        "agent_evaluator": agent_evaluator,
    }


def run_research_worker(
    *,
    session_factory: sessionmaker[Session],
    clock: Clock,
    worker_id: str,
    handlers: dict[str, Handler],
    stop: threading.Event,
    poll: float = 1.0,
) -> int:
    """Process research jobs until ``stop``. Exit 3 on database failure, like the executor."""
    runner = JobRunner(
        session_factory, handlers, clock=clock, worker_id=worker_id, kinds=RESEARCH_KINDS
    )
    _log.info("research_worker_started", worker_id=worker_id)
    try:
        while not stop.is_set():
            if not runner.run_once():
                stop.wait(poll)
    except SQLAlchemyError:
        _log.critical("research_worker_database_failure", worker_id=worker_id, exc_info=True)
        return EXIT_LEASE_LOST
    _log.info("research_worker_stopped", worker_id=worker_id)
    return EXIT_OK
