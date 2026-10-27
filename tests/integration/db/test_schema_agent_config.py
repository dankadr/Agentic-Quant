import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.db.models.research import AgentConfigProposal


def _proposal(**overrides: object) -> AgentConfigProposal:
    values: dict[str, object] = {
        "agent_name": "macro",
        "changes_json": {"run_every_minutes": 15},
        "base_json": {"run_every_minutes": 30},
        "rationale": "test",
        "status": "PENDING_REVIEW",
        "source": "OPERATOR",
        "evidence_json": None,
        "proposed_by": "op",
        "created_at": T0,
    }
    return AgentConfigProposal(**{**values, **overrides})


def test_table_exists(db: Session) -> None:
    assert "agent_config_proposals" in set(inspect(db.get_bind()).get_table_names())


def test_valid_row_round_trips(db: Session) -> None:
    row = _proposal()
    db.add(row)
    db.flush()
    db.refresh(row)
    assert (row.changes_json, row.base_json, row.reviewed_by, row.applied_at) == (
        {"run_every_minutes": 15},
        {"run_every_minutes": 30},
        None,
        None,
    )


def test_status_is_constrained(db: Session) -> None:
    db.add(_proposal(status="LIVE"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_source_is_constrained(db: Session) -> None:
    db.add(_proposal(source="ROBOT"))
    with pytest.raises(IntegrityError):
        db.flush()
