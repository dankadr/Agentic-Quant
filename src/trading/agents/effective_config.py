"""The agent settings a process actually runs with: ``configs/agents.yaml`` plus applied edits.

Edits made through the dashboard are proposals. Approval only marks them; they are applied when no
trading session is open (``apply_when_idle``), and a process picks them up only when it starts, so
settings never change under a running session. A process whose startup config is older than the
latest applied edit exits with ``EXIT_CONFIG_RELOAD`` and the supervisor restarts it.
"""

from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.config_proposals import APPLIED, APPROVED
from trading.agents.registry import AGENT_REGISTRY
from trading.core.clock import Clock
from trading.core.config import AgentsConfig, AppConfig, load_config
from trading.data.calendar import NEW_YORK, MarketCalendar
from trading.db.models.research import AgentConfigProposal
from trading.jobs.schedule import CONSENSUS_DELAY, EVALUATOR_DELAY, SESSION_START_LEAD
from trading.monitoring.audit import record_audit
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
# Apply only well clear of the jobs that straddle a session: session start (open - 30 min) and
# fundamental (open - 15 min) before it; evaluator (close + 10) and consensus (close + 12, plus an
# LLM call of up to a couple of minutes) after it. A change landing between them would run one
# session's jobs under mixed settings.
BEFORE_OPEN = SESSION_START_LEAD + timedelta(minutes=30)
AFTER_CLOSE = max(EVALUATOR_DELAY, CONSENSUS_DELAY) + timedelta(minutes=28)


def overlay_agents(
    base: AgentsConfig, session: Session, statuses: tuple[str, ...] = (APPLIED,)
) -> AgentsConfig:
    """Apply proposals in the given statuses, oldest first. A stale one is skipped, not fatal.

    Services use the default (APPLIED only). The API passes APPLIED+APPROVED to show what will
    run after the next session boundary."""
    current = base
    rows = session.scalars(
        select(AgentConfigProposal)
        .where(AgentConfigProposal.status.in_(statuses))
        .order_by(AgentConfigProposal.created_at, AgentConfigProposal.id)
    )
    for row in rows:
        info = AGENT_REGISTRY.get(row.agent_name)
        if info is None or not set(row.changes_json) <= set(info.editable):
            # Only the API writes these rows, but the table is the trust boundary: never let a
            # row name a locked scope or a field the panel does not expose.
            _log.warning("agent_config_override_rejected", proposal_id=str(row.id))
            continue
        dump = current.model_dump()
        dump[row.agent_name] = {**dump[row.agent_name], **row.changes_json}
        try:
            current = AgentsConfig.model_validate(dump)
        except (ValidationError, KeyError) as exc:
            _log.warning(
                "agent_config_override_skipped", proposal_id=str(row.id), error=type(exc).__name__
            )
    return current


UNDEFINED_TABLE = "42P01"


def config_version(session: Session) -> str | None:
    """Identifies the latest applied edit (None before any). A process remembers the version its
    settings were loaded at; a different value later means it must restart to pick up an edit."""
    latest = session.scalar(
        select(func.max(AgentConfigProposal.applied_at)).where(
            AgentConfigProposal.status == APPLIED
        )
    )
    return None if latest is None else latest.isoformat()


def config_changed(session: Session, known: str | None) -> bool:
    return config_version(session) != known


def load_effective_config(
    config_dir: Path, session_factory: sessionmaker[Session]
) -> tuple[AppConfig, str | None]:
    """The YAML config with applied edits overlaid, and the version it was loaded at.

    The version is read before the overlay, so an edit applied in between only causes one extra
    restart, never a missed one. A database error stops startup (the supervisor retries) rather
    than silently running plain YAML; only a table that does not exist yet (migration 0004 not
    run) falls back to YAML.
    """
    config = load_config(config_dir)
    try:
        with session_factory() as session:
            version = config_version(session)
            agents = overlay_agents(config.agents, session)
    except ProgrammingError as exc:
        if getattr(exc.orig, "sqlstate", None) != UNDEFINED_TABLE:
            raise
        _log.warning("agent_config_table_missing")
        return config, None
    return config.model_copy(update={"agents": agents}), version


def apply_approved_agent_config(session: Session, *, now: datetime) -> int:
    rows = session.scalars(
        select(AgentConfigProposal)
        .where(AgentConfigProposal.status == APPROVED)
        .order_by(AgentConfigProposal.created_at, AgentConfigProposal.id)
        .with_for_update()
    ).all()
    for row in rows:
        row.status = APPLIED
        row.applied_at = now
        session.flush()
        record_audit(
            session,
            actor="system",
            action="agent_config.apply",
            target=f"agent:{row.agent_name}",
            request_id=None,
            before=row.base_json,
            after=row.changes_json,
            result=APPLIED,
            now=now,
            details={"proposal_id": str(row.id)},
        )
    return len(rows)


def apply_when_idle(session: Session, clock: Clock, calendar: MarketCalendar) -> int:
    """Apply approved edits only outside [open - BEFORE_OPEN, close + AFTER_CLOSE] of a session."""
    now = clock.now()
    info = calendar.session_on(now.astimezone(NEW_YORK).date())
    if info is not None and info.open_at - BEFORE_OPEN <= now <= info.close_at + AFTER_CLOSE:
        return 0
    return apply_approved_agent_config(session, now=now)
