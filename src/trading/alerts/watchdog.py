"""Operations watchdog: checks the conditions an operator must hear about, and clears them.

It runs on the research/operations worker, not on the executor, so an executor that has stopped
cannot be the one that reports its own absence. (A complete host outage still needs a monitor on a
second host; see the runbook.)
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.alerts.store import CRITICAL, WARNING, raise_incident, resolve_incident
from trading.control.commands import CommandKind, CommandStatus
from trading.control.state import RiskState, get_system_state
from trading.core.clock import Clock
from trading.data.calendar import MarketCalendar
from trading.db.models.documents import ResearchReportRow
from trading.db.models.ops import OperationalCommandRow
from trading.jobs.lease import EXECUTOR_LEASE, current_lease

FLATTEN_STALL_AFTER = timedelta(minutes=10)
RESEARCH_FAILURE_LIMIT = 5
RESEARCH_FAILURE_WINDOW = timedelta(hours=1)


def _check(
    session: Session,
    key: str,
    *,
    condition: bool,
    severity: str,
    title: str,
    detail: str,
    now: datetime,
) -> None:
    if condition:
        raise_incident(session, key=key, severity=severity, title=title, detail=detail, now=now)
    else:
        resolve_incident(session, key=key, now=now)


def run_watchdog(
    session_factory: sessionmaker[Session],
    *,
    clock: Clock,
    calendar: MarketCalendar | None,
    market_data_state: str,
) -> None:
    """One pass over every condition. Each is raised once and cleared once."""
    now = clock.now()
    with session_factory.begin() as session:
        state = get_system_state(session)
        lease_missing = current_lease(session, EXECUTOR_LEASE, now=now) is None
        _check(
            session,
            "executor_lease_missing",
            condition=lease_missing,
            severity=CRITICAL,
            title="No executor holds the lease",
            detail="Trading cannot run. Check the worker container and its logs.",
            now=now,
        )
        _check(
            session,
            "reconciliation_required",
            condition=state.risk_state is RiskState.RECONCILIATION_REQUIRED,
            severity=CRITICAL,
            title="Reconciliation required before trading resumes",
            detail="Entries stay blocked until an operator reconciles and clears the state.",
            now=now,
        )
        _check(
            session,
            "market_data_stale",
            condition=market_data_state in ("stale", "incomplete"),
            severity=CRITICAL if market_data_state == "stale" else WARNING,
            title=f"Market data is {market_data_state}",
            detail="Entries are blocked while data is stale or incomplete.",
            now=now,
        )
        stalled = session.scalar(
            select(func.count())
            .select_from(OperationalCommandRow)
            .where(
                OperationalCommandRow.kind == CommandKind.FLATTEN.value,
                OperationalCommandRow.status == CommandStatus.IN_PROGRESS.value,
                OperationalCommandRow.requested_at < now - FLATTEN_STALL_AFTER,
            )
        )
        _check(
            session,
            "flatten_stalled",
            condition=bool(stalled),
            severity=CRITICAL,
            title="Flatten has not finished",
            detail="Positions or orders remain after the flatten command. Keep entries disabled.",
            now=now,
        )
        recent_failures = session.scalar(
            select(func.count())
            .select_from(ResearchReportRow)
            .where(
                ResearchReportRow.status == "FAILED",
                ResearchReportRow.created_at >= now - RESEARCH_FAILURE_WINDOW,
            )
        )
        _check(
            session,
            "research_failures",
            condition=(recent_failures or 0) >= RESEARCH_FAILURE_LIMIT,
            severity=WARNING,
            title="Research analysis is failing repeatedly",
            detail=f"At least {RESEARCH_FAILURE_LIMIT} analyses failed in the last hour.",
            now=now,
        )
    _ = calendar  # the market-data state already reflects the calendar; kept for the signature
