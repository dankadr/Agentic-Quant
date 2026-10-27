from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.agents.config_proposals import propose_agent_config
from trading.agents.effective_config import (
    apply_approved_agent_config,
    apply_when_idle,
    config_changed,
    config_version,
    load_effective_config,
    overlay_agents,
)
from trading.core.clock import ManualClock
from trading.core.config import load_config
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


def test_config_version_changes_when_an_edit_is_applied(db: Session) -> None:
    assert config_version(db) is None
    _proposal(db, "APPROVED")
    assert config_changed(db, None) is False  # approved is not applied
    apply_approved_agent_config(db, now=T0 + timedelta(hours=1))
    version = config_version(db)
    assert version is not None
    assert config_changed(db, None) is True
    assert config_changed(db, version) is False


def test_apply_when_idle_waits_for_the_session_to_end(
    db: Session, clock: ManualClock, calendar: MarketCalendar
) -> None:
    open_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    _proposal(db, "APPROVED")
    clock.set(open_at + timedelta(hours=2))  # mid-session
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at + timedelta(hours=6, minutes=35))  # 5 min after the close: still settling
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at + timedelta(hours=6, minutes=45))  # 20:15Z: the evaluator job just ran
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at + timedelta(hours=7, minutes=11))  # 20:41Z: 40+ min after the close
    assert apply_when_idle(db, clock, calendar) == 1


def test_apply_when_idle_never_lands_just_before_the_open(
    db: Session, clock: ManualClock, calendar: MarketCalendar
) -> None:
    open_at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    _proposal(db, "APPROVED")
    clock.set(open_at - timedelta(minutes=1))  # 13:29Z: session start and fundamental jobs ran
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at - timedelta(minutes=59))  # 12:31Z: inside the 60 min guard
    assert apply_when_idle(db, clock, calendar) == 0
    clock.set(open_at - timedelta(minutes=61))  # 12:29Z
    assert apply_when_idle(db, clock, calendar) == 1


def test_overlay_ignores_rows_that_name_locked_scopes_or_non_editable_fields(db: Session) -> None:
    row = _proposal(db, "APPLIED")
    row.agent_name, row.changes_json = "llm", {"base_url": "https://evil.example/v1"}
    other = _proposal(db, "APPLIED")
    other.changes_json = {"made_up_field": 1}
    db.flush()
    assert overlay_agents(BASE, db) == BASE


def test_load_effective_config_reads_applied_overrides_and_their_version(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    row = _proposal(db, "APPLIED")
    row.applied_at = T0 + timedelta(hours=1)
    db.commit()
    config, version = load_effective_config(Path("configs"), session_factory)
    assert config.agents.macro.run_every_minutes == 15
    assert config.risk == load_config(Path("configs")).risk  # only agents are overlaid
    assert version == config_version(db)


def test_a_database_error_fails_startup_instead_of_running_plain_yaml() -> None:
    class Down:
        def __call__(self) -> Any:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with pytest.raises(OperationalError):
        load_effective_config(Path("configs"), Down())  # type: ignore[arg-type]


def test_only_a_missing_table_falls_back_to_yaml(monkeypatch: pytest.MonkeyPatch) -> None:
    class Missing(Exception):
        sqlstate = "42P01"  # undefined_table: migration 0004 has not run yet

    class NoTable:
        def __call__(self) -> Any:
            raise ProgrammingError("SELECT 1", {}, Missing())

    config, version = load_effective_config(Path("configs"), NoTable())  # type: ignore[arg-type]
    assert (config, version) == (load_config(Path("configs")), None)
