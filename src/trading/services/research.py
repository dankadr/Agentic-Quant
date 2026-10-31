"""The research worker: model-driven agent jobs, kept out of the execution path.

It holds no executor lease, builds no broker adapter, and claims only ``RESEARCH_KINDS``. A stalled
model call therefore delays research output, never a tick, an exit, or an operator command.
"""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.effective_config import config_changed
from trading.agents.jobs import consensus_job, evaluator_job, fundamental_job, macro_job
from trading.agents.macro import JsonLlm
from trading.alerts.telegram import dispatch_due
from trading.alerts.watchdog import run_watchdog
from trading.api.routes.health import compute_readiness
from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings, read_secret
from trading.core.time import ensure_utc
from trading.data.calendar import MarketCalendar
from trading.data.news import NewsSource
from trading.documents.blobs import BlobStore
from trading.documents.fetch import Resolver, fetch, system_resolver
from trading.documents.ingest import DocumentInput, ingest
from trading.documents.sec import discover
from trading.jobs.kinds import RESEARCH_KINDS
from trading.jobs.queue import Job, enqueue
from trading.jobs.runner import Handler, JobRunner
from trading.monitoring.logging import get_logger
from trading.reports.analyze import analyze_version, enqueue_analysis
from trading.services.exit_codes import EXIT_CONFIG_RELOAD, EXIT_LEASE_LOST, EXIT_OK

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
    blobs: BlobStore | None = None
    http: httpx.Client | None = None
    resolver: Resolver | None = None


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

    def watchdog_check(job: Job) -> None:
        market = compute_readiness(ctx.session_factory, ctx.clock, ctx.calendar).market_data
        run_watchdog(
            ctx.session_factory, clock=ctx.clock, calendar=ctx.calendar, market_data_state=market
        )

    def alert_dispatch(job: Job) -> None:
        token_file = ctx.settings.telegram_token_file
        chat_id = ctx.settings.telegram_chat_id
        if token_file is None or not token_file.exists() or not chat_id:
            return  # not configured: notices stay queued and visible, nothing is sent
        token = read_secret(token_file).strip()
        if not token:
            return
        client = ctx.http or httpx.Client()
        dispatch_due(
            ctx.session_factory,
            client,
            bot_token=token,
            chat_id=chat_id,
            now=ctx.clock.now(),
        )

    def research_analyze(job: Job) -> None:
        if ctx.llm is None:
            raise RuntimeError("no model key is configured; analysis cannot run")
        analyze_version(
            ctx.session_factory,
            ctx.llm,
            config=ctx.config,
            version_id=UUID(str(job.payload["version_id"])),
            clock=ctx.clock,
        )

    def sec_discover(job: Job) -> None:
        cik = int(str(job.payload["cik"]))
        company = str(job.payload["company"])
        client = ctx.http or httpx.Client()
        for filing in discover(client, cik, ctx.config.documents):
            category = "ANALYST_REPORT" if filing.form == "8-K" else "FINANCIAL_STATEMENT"
            with ctx.session_factory.begin() as session:
                enqueue(
                    session,
                    kind="document_import",
                    payload={
                        "url": filing.url,
                        "title": f"{filing.form} filed {filing.filing_date}",
                        "category": category,
                        "company": company,
                        "source": "sec-edgar",
                        "published_at": f"{filing.filing_date}T00:00:00+00:00",
                        "reporting_period": None,
                    },
                    scheduled_at=ctx.clock.now(),
                    dedupe_key=f"document_import:sec:{filing.accession}:{filing.primary_document}",
                )

    def document_import(job: Job) -> None:
        if ctx.blobs is None:
            raise RuntimeError("no document store is configured")
        payload = job.payload
        client = ctx.http or httpx.Client()
        fetched = fetch(
            str(payload["url"]),
            ctx.config.documents,
            client=client,
            resolver=ctx.resolver or system_resolver,
        )
        published = payload.get("published_at")
        company = payload.get("company")
        reporting_period = payload.get("reporting_period")
        result = ingest(
            ctx.session_factory,
            ctx.blobs,
            ctx.config.documents,
            DocumentInput(
                title=str(payload["title"]),
                category=str(payload["category"]),
                company=None if company is None else str(company),
                source=str(payload["source"]),
                content_type=fetched.content_type,
                data=fetched.data,
                origin="URL",
                source_url=fetched.final_url,
                published_at=ensure_utc(datetime.fromisoformat(str(published)))
                if published
                else None,
                reporting_period=None if reporting_period is None else str(reporting_period),
            ),
            now=ctx.clock.now(),
        )
        if result.extraction_status == "EXTRACTED":
            with ctx.session_factory.begin() as session:
                enqueue_analysis(
                    session,
                    version_id=result.version_id,
                    model=ctx.config.agents.llm.model,
                    now=ctx.clock.now(),
                )

    return {
        "document_import": document_import,
        "research_analyze": research_analyze,
        "watchdog_check": watchdog_check,
        "alert_dispatch": alert_dispatch,
        "sec_discover": sec_discover,
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
    config_check_interval: float = 30.0,
    config_version: str | None = None,
) -> int:
    """Process research jobs until ``stop``. Exit 3 on database failure, like the executor, and
    exit 4 between jobs when an approved agent-config change was applied (restart to load it)."""
    runner = JobRunner(
        session_factory, handlers, clock=clock, worker_id=worker_id, kinds=RESEARCH_KINDS
    )
    last_config_check = time.monotonic()
    _log.info("research_worker_started", worker_id=worker_id)
    try:
        while not stop.is_set():
            worked = runner.run_once()
            if time.monotonic() - last_config_check >= config_check_interval:
                last_config_check = time.monotonic()
                with session_factory() as session:
                    reload = config_changed(session, config_version)
                if reload:  # between jobs, never during one
                    _log.info("research_worker_config_reload", worker_id=worker_id)
                    return EXIT_CONFIG_RELOAD
            if not worked:
                stop.wait(poll)
    except SQLAlchemyError:
        _log.critical("research_worker_database_failure", worker_id=worker_id, exc_info=True)
        return EXIT_LEASE_LOST
    _log.info("research_worker_stopped", worker_id=worker_id)
    return EXIT_OK
