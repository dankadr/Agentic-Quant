from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.agents.config_proposals import (
    ProposalConflict,
    ProposalInvalid,
    ProposalNotFound,
    propose_agent_config,
    review_agent_config,
)
from trading.core.config import AgentsConfig

EFFECTIVE = AgentsConfig()


def propose(
    db: Session,
    changes: dict,
    who: str = "op",
    agent: str = "macro",
    effective: AgentsConfig = EFFECTIVE,
):  # type: ignore[no-untyped-def,type-arg]
    return propose_agent_config(
        db, agent_name=agent, changes=changes, rationale="r", proposed_by=who,
        effective=effective, now=T0,
    )  # fmt: skip


def test_valid_edit_stores_diff(db: Session) -> None:
    row = propose(db, {"run_every_minutes": 15})
    assert (row.status, row.source, row.agent_name) == ("PENDING_REVIEW", "OPERATOR", "macro")
    assert row.changes_json == {"run_every_minutes": 15}
    assert row.base_json == {"run_every_minutes": 30}


def test_list_field_is_accepted_and_stored_as_list(db: Session) -> None:
    row = propose(db, {"dependent_strategies": ["orb_v1"]})
    assert row.base_json == {"dependent_strategies": ["orb_v1", "vwap_v1"]}


@pytest.mark.parametrize("agent,field", [("macro", "llm"), ("macro", "risk"), ("macro", "mode")])
def test_locked_or_unknown_field_refused(db: Session, agent: str, field: str) -> None:
    with pytest.raises(ProposalInvalid):
        propose(db, {field: 1}, agent=agent)


def test_locked_scope_as_agent_refused_and_unknown_agent_not_found(db: Session) -> None:
    with pytest.raises(ProposalInvalid):
        propose(db, {"max_gross_exposure_pct": "1"}, agent="risk")
    with pytest.raises(ProposalNotFound):
        propose(db, {"enabled": True}, agent="nonexistent")


def test_cross_field_rule_refused(db: Session) -> None:
    with pytest.raises(ProposalInvalid, match="ttl"):
        propose(db, {"ttl_minutes": 20, "run_every_minutes": 30})
    assert db.execute(text("select count(*) from agent_config_proposals")).scalar_one() == 0


def test_noop_refused(db: Session) -> None:
    with pytest.raises(ProposalInvalid, match="no change"):
        propose(db, {"run_every_minutes": 30})


def test_wrong_types_refused(db: Session) -> None:
    with pytest.raises(ProposalInvalid):
        propose(db, {"run_every_minutes": 15.5})
    with pytest.raises(ProposalInvalid):
        propose(db, {"run_every_minutes": True})
    with pytest.raises(ProposalInvalid):
        propose(db, {"enabled": 1})
    with pytest.raises(ProposalInvalid):
        propose(db, {"run_every_minutes": 0})  # outside the field's bounds


def test_empty_changes_refused(db: Session) -> None:
    with pytest.raises(ProposalInvalid):
        propose(db, {})


def test_newer_supersedes_only_same_proposer(db: Session) -> None:
    mine = propose(db, {"run_every_minutes": 15}, who="me")
    theirs = propose(db, {"run_every_minutes": 20}, who="them")
    newer = propose(db, {"run_every_minutes": 10}, who="me")
    db.refresh(mine), db.refresh(theirs)
    assert (mine.status, theirs.status, newer.status) == (
        "SUPERSEDED", "PENDING_REVIEW", "PENDING_REVIEW"
    )  # fmt: skip


def test_review_approves_and_audits(db: Session) -> None:
    row = propose(db, {"run_every_minutes": 15})
    done = review_agent_config(
        db, row.id, approve=True, reviewer="op", now=T0 + timedelta(minutes=1), effective=EFFECTIVE
    )
    assert (done.status, done.reviewed_by) == ("APPROVED", "op")
    actions = db.execute(text("select action from audit_events order by ts, id")).scalars().all()
    assert actions == ["agent_config.propose", "agent_config.approve"]


def test_review_reject_marks_rejected(db: Session) -> None:
    row = propose(db, {"run_every_minutes": 15})
    done = review_agent_config(
        db, row.id, approve=False, reviewer="op", now=T0, effective=EFFECTIVE
    )
    assert done.status == "REJECTED"


def test_review_of_non_pending_or_unknown_is_refused(db: Session) -> None:
    row = propose(db, {"run_every_minutes": 15})
    review_agent_config(db, row.id, approve=True, reviewer="op", now=T0, effective=EFFECTIVE)
    with pytest.raises(ProposalConflict):
        review_agent_config(db, row.id, approve=True, reviewer="op", now=T0, effective=EFFECTIVE)
    with pytest.raises(ProposalNotFound):
        review_agent_config(db, uuid4(), approve=True, reviewer="op", now=T0, effective=EFFECTIVE)


def test_stale_base_refused_on_approval(db: Session) -> None:
    row = propose(db, {"run_every_minutes": 15})
    changed = AgentsConfig.model_validate(
        {
            **EFFECTIVE.model_dump(),
            "macro": {**EFFECTIVE.macro.model_dump(), "run_every_minutes": 20},
        }
    )
    with pytest.raises(ProposalConflict, match="stale"):
        review_agent_config(db, row.id, approve=True, reviewer="op", now=T0, effective=changed)
    db.refresh(row)
    assert row.status == "PENDING_REVIEW"
    # rejecting a stale proposal is always allowed
    review_agent_config(db, row.id, approve=False, reviewer="op", now=T0, effective=changed)


def test_approval_revalidates_the_combined_settings(db: Session) -> None:
    ttl = propose(db, {"ttl_minutes": 40}, who="a")  # alone: 40 > 30, fine
    run = propose(db, {"run_every_minutes": 45}, who="b")  # alone: 45 < 60, fine
    review_agent_config(db, ttl.id, approve=True, reviewer="op", now=T0, effective=EFFECTIVE)
    approved = AgentsConfig.model_validate(  # what the API passes: projected incl. the approval
        {**EFFECTIVE.model_dump(), "macro": {**EFFECTIVE.macro.model_dump(), "ttl_minutes": 40}}
    )
    with pytest.raises(ProposalConflict, match="combined"):
        review_agent_config(db, run.id, approve=True, reviewer="op", now=T0, effective=approved)
    db.refresh(run)
    assert run.status == "PENDING_REVIEW"
