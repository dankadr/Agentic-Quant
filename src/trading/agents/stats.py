"""Read-only views of agent runs for the dashboard: per-agent numbers and per-decision "why".

Everything in ``result_json`` is model output or derived from it, so it is treated as untrusted:
missing or oddly typed keys never raise, and nothing here is rendered as HTML.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.config import AgentsConfig
from trading.core.time import ensure_utc
from trading.data.calendar import NEW_YORK, CalendarUnavailable, MarketCalendar
from trading.db.models.research import AgentRun
from trading.db.models.trading import OrderIntent
from trading.jobs.schedule import plan_session_jobs

Health = Literal["ok", "degraded", "failing", "idle"]
_BAD = ("REJECTED_INVALID", "FAILED")
MACRO_STRATEGIES = ("orb_v1", "vwap_v1")


@dataclass(frozen=True)
class AgentSummary:
    name: str
    runs: int
    completed: int
    rejected: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    last_run_at: datetime | None
    last_error: str | None
    health: Health
    skipped: int = 0
    last_success_at: datetime | None = None
    last_skip_reason: str | None = None
    next_scheduled_run: datetime | None = None
    schedule_state: str = "unknown"
    data_age_seconds: float | None = None


@dataclass(frozen=True)
class Decision:
    run_id: UUID
    at: datetime
    status: str
    headline: str
    confidence: Decimal | None
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    error: str | None
    input_hash: str | None
    affected_orders: tuple[str, ...]


def _result(run: AgentRun) -> dict[str, Any]:
    return run.result_json if isinstance(run.result_json, dict) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _decimal(value: object) -> Decimal | None:
    """Model output stores numbers as JSON numbers or strings; NaN, infinity and bools are not."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        return None
    return out if out.is_finite() else None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _schedule_context(
    name: str,
    now: datetime,
    calendar: MarketCalendar | None,
    agents: AgentsConfig | None,
) -> tuple[datetime | None, datetime | None, str]:
    if agents is None or calendar is None:
        return None, None, "unknown"
    if not getattr(getattr(agents, name, None), "enabled", False):
        return None, None, "disabled"
    if name == "technical":
        at = now.astimezone(UTC).replace(hour=8, minute=0, second=0, microsecond=0)
        at += timedelta(days=(5 - at.weekday()) % 7)
        if at <= now:
            at += timedelta(days=7)
        return at, None, "waiting_for_research"
    day = now.astimezone(NEW_YORK).date()
    try:
        current = calendar.session_on(day)
        following = calendar.next_session_after(day)
        sessions = [s for s in (current, following) if s is not None]
        jobs = [
            j
            for s in sessions
            for j in plan_session_jobs(s, agents=agents)
            if j.kind == f"agent_{name}"
        ]
        next_at = min((j.scheduled_at for j in jobs if j.scheduled_at > now), default=None)
        expected = max((j.scheduled_at for j in jobs if j.scheduled_at <= now), default=None)
        outside = current is None or not current.open_at <= now <= current.close_at + timedelta(
            minutes=20
        )
        return next_at, expected, "outside_schedule" if outside else "scheduled"
    except CalendarUnavailable:
        return None, None, "calendar_unavailable"


def agent_summary(
    session: Session,
    name: str,
    *,
    since: datetime,
    now: datetime | None = None,
    calendar: MarketCalendar | None = None,
    agents: AgentsConfig | None = None,
) -> AgentSummary:
    query = select(AgentRun).where(AgentRun.agent_name == name)
    if now is not None:
        query = query.where(AgentRun.started_at <= now)
    query = query.order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
    runs = list(session.scalars(query.where(AgentRun.started_at >= since)))
    latest = session.scalar(query.limit(1))
    success = session.scalar(query.where(AgentRun.status == "COMPLETED").limit(1))
    skipped = session.scalar(query.where(AgentRun.status == "SKIPPED").limit(1))
    next_at, expected, schedule = _schedule_context(
        name, now or since + timedelta(hours=24), calendar, agents
    )
    tokens = [_result(r).get("tokens") for r in runs]
    prompt = sum(_int(t.get("prompt")) for t in tokens if isinstance(t, dict))
    completion = sum(_int(t.get("completion")) for t in tokens if isinstance(t, dict))
    health: Health = "idle"
    if runs:
        health = (
            "failing"
            if runs[0].status in _BAD
            else "degraded"
            if any(r.status in _BAD for r in runs)
            else "idle"
            if runs[0].status == "SKIPPED"
            else "ok"
        )
    # Missing an expected run is meaningful only during its scheduled session, with grace
    # for the worker queue. Weekends and prerequisite waits never manufacture failures.
    if schedule == "scheduled" and expected is not None and now is not None:
        if now > expected + timedelta(minutes=2 if name == "data_quality" else 10) and (
            latest is None or latest.started_at < expected
        ):
            health, schedule = "degraded", "overdue"
    observed = _when(_result(latest).get("data_at")) if latest else None
    age = max(0.0, (now - ensure_utc(observed)).total_seconds()) if now and observed else None
    return AgentSummary(
        name=name,
        runs=len(runs),
        completed=sum(r.status == "COMPLETED" for r in runs),
        rejected=sum(r.status == "REJECTED_INVALID" for r in runs),
        failed=sum(r.status == "FAILED" for r in runs),
        prompt_tokens=prompt,
        completion_tokens=completion,
        last_run_at=latest.started_at if latest else None,
        last_error=next((e for r in runs if (e := _text(_result(r).get("error")))), None),
        health=health,
        skipped=sum(r.status == "SKIPPED" for r in runs),
        last_success_at=success.completed_at if success else None,
        last_skip_reason=_text(_result(skipped).get("skip_reason")) if skipped else None,
        next_scheduled_run=next_at,
        schedule_state=schedule,
        data_age_seconds=age,
    )


def _affected(session: Session, run: AgentRun, strategies: Sequence[str]) -> tuple[str, ...]:
    until = _when(_result(run).get("valid_until"))
    if until is None:
        return ()
    intents = session.scalars(
        select(OrderIntent)
        .where(
            OrderIntent.is_entry.is_(True),
            OrderIntent.created_at >= run.started_at,
            OrderIntent.created_at < until,
        )
        .order_by(OrderIntent.created_at)
    )
    return tuple(i.client_order_id for i in intents if set(i.strategy_ids) & set(strategies))


def agent_decisions(
    session: Session,
    name: str,
    *,
    limit: int = 30,
    macro_strategies: Sequence[str] = MACRO_STRATEGIES,
) -> list[Decision]:
    runs = session.scalars(
        select(AgentRun)
        .where(AgentRun.agent_name == name)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
        .limit(limit)
    )
    out: list[Decision] = []
    for run in runs:
        result = _result(run)
        error = _text(result.get("error"))
        out.append(
            Decision(
                run_id=run.id,
                at=run.started_at,
                status=run.status,
                headline=_text(result.get("conclusion"))
                or _text(result.get("regime"))
                or _text(result.get("summary"))
                or _text(result.get("skip_reason"))
                or error
                or run.status,
                confidence=_decimal(result.get("confidence")),
                evidence=_strings(result.get("evidence")),
                limitations=_strings(result.get("limitations")),
                error=error,
                input_hash=_text(result.get("input_hash")),
                affected_orders=_affected(session, run, macro_strategies)
                if name == "macro"
                else (),
            )
        )
    return out
