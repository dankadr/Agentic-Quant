"""Job-level wrappers for the agents: enablement, a configured model, and staleness checks."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trading.agents.consensus import run_consensus_agent
from trading.agents.evaluator import run_evaluator_agent
from trading.agents.fundamental import run_fundamental_agent
from trading.agents.macro import JsonLlm, run_macro_agent
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.time import ensure_utc
from trading.data.calendar import MarketCalendar
from trading.data.news import NewsSource
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
MAX_JOB_LAG = timedelta(minutes=10)


def macro_job(
    session_factory: sessionmaker[Session],
    llm: JsonLlm | None,
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    clock: Clock,
    payload: Mapping[str, Any],
) -> None:
    if not config.agents.macro.enabled:
        return
    if llm is None:
        _log.warning("agent_macro_skipped", reason="no llm key configured")
        return
    now = clock.now()
    if now - ensure_utc(datetime.fromisoformat(str(payload["at"]))) > MAX_JOB_LAG:
        _log.warning("agent_macro_skipped", reason="stale job")
        return
    status = run_macro_agent(
        session_factory, llm, config=config, calendar=calendar, provider_name=provider_name, now=now
    )
    _log.info("agent_macro_done", status=status)


def evaluator_job(
    session_factory: sessionmaker[Session],
    llm: JsonLlm | None,
    *,
    config: AppConfig,
    provider_name: str,
    clock: Clock,
    payload: Mapping[str, Any],
) -> None:
    if not config.agents.evaluator.enabled:
        return
    if llm is None:
        _log.warning("agent_evaluator_skipped", reason="no llm key configured")
        return
    status = run_evaluator_agent(
        session_factory,
        llm,
        config=config,
        session_date=date.fromisoformat(str(payload["session_date"])),
        provider_name=provider_name,
        now=clock.now(),
    )
    _log.info("agent_evaluator_done", status=status)


def fundamental_job(
    session_factory: sessionmaker[Session],
    llm: JsonLlm | None,
    news: NewsSource | None,
    *,
    config: AppConfig,
    clock: Clock,
) -> None:
    if not config.agents.fundamental.enabled:
        return
    if llm is None or news is None:
        _log.warning("agent_fundamental_skipped", reason="no llm key or news source")
        return
    status = run_fundamental_agent(session_factory, llm, news, config=config, now=clock.now())
    _log.info("agent_fundamental_done", status=status)


def consensus_job(
    session_factory: sessionmaker[Session],
    *,
    config: AppConfig,
    clock: Clock,
    payload: Mapping[str, Any],
) -> None:
    if not config.agents.consensus.enabled:
        return
    status = run_consensus_agent(
        session_factory,
        config=config,
        session_date=date.fromisoformat(str(payload["session_date"])),
        now=clock.now(),
    )
    _log.info("agent_consensus_done", status=status)
