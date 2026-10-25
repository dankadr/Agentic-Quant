from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.core.config import load_config
from trading.quant.proposals import (
    ProposalConflict,
    ProposalForbidden,
    ProposalInvalid,
    ProposalNotFound,
    apply_approved,
    propose_config,
    review_proposal,
)
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))
UNIVERSE = frozenset(u.symbol for u in CFG.universe)


@pytest.fixture(autouse=True)
def _seeded(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    db.flush()


def _propose(db: Session, params: dict[str, object] | None = None, **kw):  # type: ignore[no-untyped-def]
    return propose_config(
        db,
        strategy_id=kw.pop("strategy_id", "orb_v1"),
        base_version=kw.pop("base_version", "1.0.0"),
        parameters=params if params is not None else {"target_r": "2.0"},
        rationale="walk-forward supported",
        proposed_by=kw.pop("proposed_by", "agent"),
        universe=UNIVERSE,
        now=T0,
    )


def _params(db: Session) -> tuple[str, dict[str, object]]:
    row = db.execute(text("select version, params_json from strategies where id = 'orb_v1'")).one()
    return row.version, row.params_json


def test_a_proposal_is_stored_pending_and_changes_nothing(db: Session) -> None:
    before = _params(db)
    p = _propose(db)
    assert p.status == "PENDING_REVIEW"
    assert p.parameters_json == {"target_r": "2.0"}
    assert _params(db) == before


@pytest.mark.parametrize(
    "params",
    [
        {"target_r": "-1"},  # out of range
        {"no_such_param": "1"},  # unknown key
        {"target_r": 1.5},  # float money/precision is refused
        {"symbols": ["SPY", "BTCUSD"]},  # outside the configured universe
        {},  # nothing to change
    ],
)
def test_invalid_parameters_are_rejected(db: Session, params: dict[str, object]) -> None:
    with pytest.raises(ProposalInvalid):
        _propose(db, params)
    assert db.execute(text("select count(*) from strategy_config_proposals")).scalar_one() == 0


def test_unknown_strategy_and_stale_base_version(db: Session) -> None:
    with pytest.raises(ProposalNotFound):
        _propose(db, strategy_id="nope")
    with pytest.raises(ProposalConflict):
        _propose(db, base_version="0.9.0")


def test_only_tunable_strategies_accept_proposals(db: Session) -> None:
    with pytest.raises(ProposalInvalid):
        _propose(db, {"x": "1"}, strategy_id="consensus_shadow", base_version="1.0.0")


def test_a_new_proposal_supersedes_the_older_pending_one(db: Session) -> None:
    first = _propose(db, {"target_r": "2.0"})
    second = _propose(db, {"target_r": "1.8"})
    db.refresh(first)
    assert (first.status, second.status) == ("SUPERSEDED", "PENDING_REVIEW")


def test_the_proposer_cannot_approve_its_own_proposal(db: Session) -> None:
    p = _propose(db, proposed_by="agent")
    with pytest.raises(ProposalForbidden):
        review_proposal(db, p.id, approve=True, reviewer="agent", now=T0)
    assert p.status == "PENDING_REVIEW"


def test_review_decisions_are_final(db: Session) -> None:
    p = _propose(db)
    review_proposal(db, p.id, approve=True, reviewer="human", now=T0)
    assert (p.status, p.reviewed_by) == ("APPROVED", "human")
    with pytest.raises(ProposalConflict):
        review_proposal(db, p.id, approve=False, reviewer="human", now=T0)
    with pytest.raises(ProposalNotFound):
        review_proposal(db, p.id.__class__(int=1), approve=True, reviewer="human", now=T0)


def test_rejected_and_unreviewed_proposals_are_never_applied(db: Session) -> None:
    pending = _propose(db, {"target_r": "2.0"})
    rejected = _propose(db, {"target_r": "1.9"})
    review_proposal(db, rejected.id, approve=False, reviewer="human", now=T0)
    before = _params(db)
    assert apply_approved(db, now=T0 + timedelta(days=1)) == []
    assert _params(db) == before
    assert (pending.status, rejected.status) == ("SUPERSEDED", "REJECTED")


def test_an_approved_proposal_is_applied_with_a_version_bump_and_keeps_other_params(
    db: Session,
) -> None:
    p = _propose(db, {"target_r": "2.0"})
    review_proposal(db, p.id, approve=True, reviewer="human", now=T0)
    assert _params(db)[1]["target_r"] == "1.5"  # approval alone changes nothing
    applied = apply_approved(db, now=T0 + timedelta(days=1))
    assert [a.id for a in applied] == [p.id]
    version, params = _params(db)
    assert version == "1.0.1"
    assert params["target_r"] == "2.0"
    assert params["trade_notional"] == "80"
    assert (p.status, p.applied_version) == ("APPLIED", "1.0.1")
    assert p.applied_at == T0 + timedelta(days=1)
    assert apply_approved(db, now=T0 + timedelta(days=2)) == []  # applied exactly once


def test_an_approved_proposal_on_a_changed_base_is_superseded_not_applied(db: Session) -> None:
    p = _propose(db, {"target_r": "2.0"})
    review_proposal(db, p.id, approve=True, reviewer="human", now=T0)
    db.execute(text("update strategies set version = '1.1.0' where id = 'orb_v1'"))
    assert apply_approved(db, now=T0 + timedelta(days=1)) == []
    assert p.status == "SUPERSEDED"
    assert _params(db)[1]["target_r"] == "1.5"


def test_every_step_is_audited(db: Session) -> None:
    p = _propose(db)
    review_proposal(db, p.id, approve=True, reviewer="human", now=T0)
    apply_approved(db, now=T0 + timedelta(days=1))
    actions = db.execute(text("select action from audit_events order by ts, id")).scalars().all()
    assert {"strategy_config.propose", "strategy_config.approve", "strategy_config.apply"} <= set(
        actions
    )


def test_cross_field_strategy_constraints_are_invalid_not_a_server_error(db: Session) -> None:
    with pytest.raises(ProposalInvalid):
        _propose(db, {"z_stop": "1.0"}, strategy_id="vwap_v1")  # z_stop must exceed z_entry


def test_a_new_proposal_only_supersedes_the_same_proposers_pending_one(db: Session) -> None:
    """Security review: one proposer (e.g. an agent) must not silently discard another's work."""
    human = _propose(db, {"target_r": "2.0"}, proposed_by="alice")
    agent = _propose(db, {"target_r": "1.8"}, proposed_by="agent:technical")
    db.refresh(human)
    assert (human.status, agent.status) == ("PENDING_REVIEW", "PENDING_REVIEW")
    newer_agent = _propose(db, {"target_r": "1.9"}, proposed_by="agent:technical")
    db.refresh(agent)
    assert (agent.status, newer_agent.status, human.status) == (
        "SUPERSEDED",
        "PENDING_REVIEW",
        "PENDING_REVIEW",
    )
