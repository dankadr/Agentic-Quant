"""Job-level wrappers for the agents: enablement, a configured model, and staleness checks."""

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from trading.agents.evaluator import run_evaluator_agent
from trading.agents.macro import JsonLlm, run_macro_agent
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.time import ensure_utc
from trading.data.calendar import MarketCalendar
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
