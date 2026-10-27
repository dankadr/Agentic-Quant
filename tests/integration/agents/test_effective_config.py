from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.agents.config_proposals import propose_agent_config
from trading.agents.effective_config import (
    apply_approved_agent_config,
    apply_when_idle,
    config_changed_since,
    load_effective_config,
    overlay_agents,
)
from trading.core.clock import ManualClock
from trading.core.config import AgentsConfig, load_config
from trading.data.calendar import MarketCalendar
from trading.db.models.research import AgentConfigProposal

BASE = load_config(Path("configs")).agents


def _proposal(db: Session, status: str, **changes: object) -> AgentConfigProposal:
    row = propose_agent_config(
        db, agent_name="macro", changes=changes or {"run_every_minutes": 15},
        rationale="r", proposed_by="op", effective=BASE, now=T0,
    )  # fmt: skip
    row.status = status
    db.flush()
    return row


def test_overlay_changes_macro_interval(db: Session) -> None:
    _proposal(db, "APPLIED")
    assert BASE.macro.run_every_minutes == 30
    assert overlay_agents(BASE, db).macro.run_every_minutes == 15


def test_only_applied_proposals_are_overlaid(db: Session) -> None:
    for status in ("PENDING_REVIEW", "APPROVED", "REJECTED", "SUPERSEDED"):
        _proposal(db, status)
    assert overlay_agents(BASE, db) == BASE


def test_later_applied_proposal_wins_and_fields_merge(db: Session) -> None:
    _proposal(db, "APPLIED", run_every_minutes=15)
    later = _proposal(db, "APPLIED", run_every_minutes=20, required=True)
    later.created_at = T0 + timedelta(minutes=1)
    db.flush()
    macro = overlay_agents(BASE, db).macro
    assert (macro.run_every_minutes, macro.required) == (20, True)


def test_invalid_override_is_skipped_not_fatal(db: Session) -> None:
    row = _proposal(db, "APPLIED")
    row.changes_json = {"ttl_minutes": 5}  # breaks ttl > run_every_minutes on a later config
    db.flush()
    assert overlay_agents(BASE, db) == BASE


def test_apply_marks_applied_once(db: Session) -> None:
    row = _proposal(db, "APPROVED")
    assert apply_approved_agent_config(db, now=T0 + timedelta(hours=1)) == 1
    db.refresh(row)
    assert (row.status, row.applied_at) == ("APPLIED", T0 + timedelta(hours=1))
    assert apply_approved_agent_config(db, now=T0 + timedelta(hours=2)) == 0
    assert overlay_agents(BASE, db).macro.run_every_minutes == 15


def test_config_changed_since(db: Session) -> None:
    _proposal(db, "APPROVED")
    apply_approved_agent_config(db, now=T0 + timedelta(hours=1))
    assert config_changed_since(db, T0) is True
    assert config_changed_since(db, T0 + timedelta(hours=2)) is False


def test_apply_when_idle_waits_for_the_session_to_end(
    db: Session, clock: ManualClock, calendar: MarketCalendar
) -> None:
    open_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    _proposal(db, "APPROVED")
    clock.set(open_at + timedelta(hours=2))  # mid-session
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at + timedelta(hours=6, minutes=35))  # 5 min after the close: still settling
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at + timedelta(hours=6, minutes=45))  # 10+ min after the close
    assert apply_when_idle(db, clock, calendar) == 1


def test_load_effective_config_reads_applied_overrides(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    _proposal(db, "APPLIED")
    db.commit()
    config = load_effective_config(Path("configs"), session_factory)
    assert config.agents.macro.run_every_minutes == 15
    assert isinstance(config.agents, AgentsConfig)
    assert config.risk == load_config(Path("configs")).risk  # only agents are overlaid
