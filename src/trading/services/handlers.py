"""Job handlers run by the trading worker."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from trading.agents.jobs import consensus_job, evaluator_job, fundamental_job, macro_job
from trading.agents.macro import JsonLlm
from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings
from trading.core.time import ensure_utc, isoformat_z
from trading.data.news import NewsSource
from trading.jobs.lease import EXECUTOR_LEASE, Lease, assert_fence
from trading.jobs.outbox import dispatch_pending
from trading.jobs.queue import Job
from trading.jobs.runner import Handler
from trading.monitoring.audit import record_audit
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import LEASE_HELD
from trading.services.bootstrap import TradingRuntime

_log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerContext:
    settings: Settings
    config: AppConfig
    session_factory: sessionmaker[Session]
    clock: Clock
    lease: Lease
    runtime: TradingRuntime | None = None
    llm: JsonLlm | None = None
    news: NewsSource | None = None


def reconcile_on_start(ctx: WorkerContext) -> None:
    """Worker startup hook: reconcile cash, positions and orders before any job runs."""
    if ctx.runtime is not None:
        ctx.runtime.processor.reconcile()


def build_handlers(ctx: WorkerContext) -> dict[str, Handler]:
    def heartbeat(job: Job) -> None:
        LEASE_HELD.labels(EXECUTOR_LEASE).set(1)
        _log.info("heartbeat", job_id=str(job.id), fencing_token=ctx.lease.fencing_token)

    handlers: dict[str, Handler] = {"heartbeat": heartbeat}
    runtime = ctx.runtime
    if runtime is None:
        return handlers
    processor = runtime.processor
    stale_after = timedelta(seconds=ctx.config.risk.max_bar_age_seconds)

    def session_start(job: Job) -> None:
        processor.start_session(date.fromisoformat(str(job.payload["session_date"])))

    def paper_tick(job: Job) -> None:
        minute = ensure_utc(datetime.fromisoformat(str(job.payload["minute"])))
        lag = ctx.clock.now() - minute
        if lag > stale_after:
            # Acting on an old minute is what the freshness rules forbid: skip, never catch up.
            with ctx.session_factory.begin() as session:
                now = ctx.clock.now()
                assert_fence(session, ctx.lease, now=now)
                record_audit(
                    session,
                    actor="system",
                    action="tick_skipped_stale",
                    target=isoformat_z(minute),
                    request_id=None,
                    before=None,
                    after=None,
                    result="skipped",
                    now=now,
                    details={"lag_seconds": int(lag.total_seconds())},
                )
            return
        if processor.provider is None:
            _log.info("tick_skipped", reason="market_data: not_configured")
            return
        processor.run_tick(minute)

    def session_end(job: Job) -> None:
        processor.end_session(date.fromisoformat(str(job.payload["session_date"])))

    def reconcile(job: Job) -> None:
        processor.reconcile()

    def outbox_dispatch(job: Job) -> None:
        with ctx.session_factory.begin() as session:
            dispatch_pending(session, {}, now=ctx.clock.now())

    def agent_macro(job: Job) -> None:
        macro_job(
            ctx.session_factory,
            ctx.llm,
            config=ctx.config,
            calendar=runtime.calendar,
            provider_name=processor.provider_name,
            clock=ctx.clock,
            payload=job.payload,
        )

    def agent_evaluator(job: Job) -> None:
        evaluator_job(
            ctx.session_factory,
            ctx.llm,
            config=ctx.config,
            provider_name=processor.provider_name,
            clock=ctx.clock,
            payload=job.payload,
        )

    def agent_fundamental(job: Job) -> None:
        fundamental_job(ctx.session_factory, ctx.llm, ctx.news, config=ctx.config, clock=ctx.clock)

    def agent_consensus(job: Job) -> None:
        consensus_job(ctx.session_factory, config=ctx.config, clock=ctx.clock, payload=job.payload)

    handlers.update(
        {
            "agent_fundamental": agent_fundamental,
            "agent_consensus": agent_consensus,
            "agent_macro": agent_macro,
            "agent_evaluator": agent_evaluator,
            "session_start": session_start,
            "paper_tick": paper_tick,
            "session_end": session_end,
            "reconcile": reconcile,
            "outbox_dispatch": outbox_dispatch,
        }
    )
    return handlers
