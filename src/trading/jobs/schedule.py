"""Pure job planning: what should exist in the queue at ``now``. The scheduler enqueues it.

Per trading session: ``session_start`` (30 min before the open), one ``paper_tick`` per regular
session minute (5 s after the bar closes, so the bar is available), ``session_end`` (5 min after
the close). Only jobs due in the last two minutes are planned: a missed tick is never backfilled,
because acting on stale data is exactly what the risk engine forbids.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trading.core.config import AgentsConfig
from trading.core.time import ensure_utc, isoformat_z
from trading.data.calendar import NEW_YORK, CalendarUnavailable, MarketCalendar, SessionInfo
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
SESSION_START_LEAD = timedelta(minutes=30)
TICK_DELAY = timedelta(seconds=5)
SESSION_END_DELAY = timedelta(minutes=5)
PLANNING_WINDOW = timedelta(minutes=2)
AGENT_DELAY = timedelta(seconds=20)  # after the tick of the same minute has ingested its bars
MACRO_LAST_BEFORE_CLOSE = timedelta(minutes=30)
EVALUATOR_DELAY = timedelta(minutes=10)
CONSENSUS_DELAY = timedelta(minutes=12)
FUNDAMENTAL_LEAD = timedelta(minutes=15)
_ONE_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class JobSpec:
    kind: str
    dedupe_key: str
    scheduled_at: datetime
    payload: Mapping[str, Any]


def plan_session_jobs(session: SessionInfo, *, agents: AgentsConfig | None = None) -> list[JobSpec]:
    """Every job of one trading session, regardless of the current time."""
    day = session.session_date.isoformat()
    jobs = [
        JobSpec(
            "session_start",
            f"session_start:{day}",
            session.open_at - SESSION_START_LEAD,
            {"session_date": day},
        )
    ]
    minute = session.open_at + _ONE_MINUTE
    while minute <= session.close_at:
        stamp = isoformat_z(minute)
        jobs.append(
            JobSpec("paper_tick", f"paper_tick:{stamp}", minute + TICK_DELAY, {"minute": stamp})
        )
        minute += _ONE_MINUTE
    if agents is not None and agents.macro.enabled:
        interval = timedelta(minutes=agents.macro.run_every_minutes)
        at = session.open_at + timedelta(minutes=agents.macro.first_run_after_open_minutes)
        while at <= session.close_at - MACRO_LAST_BEFORE_CLOSE:
            jobs.append(
                JobSpec(
                    "agent_macro",
                    f"agent_macro:{isoformat_z(at)}",
                    at + AGENT_DELAY,
                    {"at": isoformat_z(at)},
                )
            )
            at += interval
    if agents is not None and agents.fundamental.enabled:
        jobs.append(
            JobSpec(
                "agent_fundamental",
                f"agent_fundamental:{day}",
                session.open_at - FUNDAMENTAL_LEAD,
                {"session_date": day},
            )
        )
    if agents is not None and agents.consensus.enabled:
        jobs.append(
            JobSpec(
                "agent_consensus",
                f"agent_consensus:{day}",
                session.close_at + CONSENSUS_DELAY,
                {"session_date": day},
            )
        )
    if agents is not None and agents.evaluator.enabled:
        jobs.append(
            JobSpec(
                "agent_evaluator",
                f"agent_evaluator:{day}",
                session.close_at + EVALUATOR_DELAY,
                {"session_date": day},
            )
        )
    jobs.append(
        JobSpec(
            "session_end",
            f"session_end:{day}",
            session.close_at + SESSION_END_DELAY,
            {"session_date": day},
        )
    )
    return jobs


def plan_jobs(
    now: datetime, calendar: MarketCalendar | None, *, agents: AgentsConfig | None = None
) -> list[JobSpec]:
    """Jobs due at ``now``. Dedupe keys make repeated planning harmless."""
    now = ensure_utc(now)
    minute = now.replace(second=0, microsecond=0)
    jobs = [
        JobSpec(
            kind="heartbeat",
            dedupe_key=f"heartbeat:{minute:%Y-%m-%dT%H:%MZ}",
            scheduled_at=minute,
            payload={},
        ),
        JobSpec(
            kind="watchdog_check",
            dedupe_key=f"watchdog_check:{minute:%Y-%m-%dT%H:%MZ}",
            scheduled_at=minute,
            payload={},
        ),
        JobSpec(
            kind="alert_dispatch",
            dedupe_key=f"alert_dispatch:{minute:%Y-%m-%dT%H:%MZ}",
            scheduled_at=minute,
            payload={},
        ),
    ]
    if calendar is None:
        return jobs
    try:
        session = calendar.session_on(now.astimezone(NEW_YORK).date())
    except CalendarUnavailable:
        _log.error("calendar_unavailable_no_session_jobs", now=isoformat_z(now))
        return jobs
    if session is None:
        return jobs
    jobs.extend(
        job
        for job in plan_session_jobs(session, agents=agents)
        if now - PLANNING_WINDOW <= job.scheduled_at <= now
    )
    return jobs
