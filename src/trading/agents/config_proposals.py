"""Reviewed edits to agent settings: propose -> approve -> applied at the next session start.

A proposal never changes a running process. Only the fields an agent exposes in the registry can
be edited; risk limits, broker, universe, strategies and the LLM endpoint are unreachable. Values
are validated through the same frozen config models the services load at startup, so cross-field
rules (for example macro ``ttl_minutes > run_every_minutes``) hold for every stored proposal.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.agents.registry import AGENT_REGISTRY, LOCKED_SCOPES
from trading.core.config import AgentsConfig
from trading.core.errors import TradingError
from trading.db.models.research import AgentConfigProposal
from trading.monitoring.audit import record_audit

PENDING, APPROVED, REJECTED, APPLIED, SUPERSEDED = (
    "PENDING_REVIEW",
    "APPROVED",
    "REJECTED",
    "APPLIED",
    "SUPERSEDED",
)
SOURCES = ("OPERATOR", "SUGGESTION")


class ProposalError(TradingError):
    code = "AGENT_CONFIG_ERROR"


class ProposalNotFound(ProposalError):
    code = "NOT_FOUND"


class ProposalConflict(ProposalError):
    code = "CONFLICT"


class ProposalInvalid(ProposalError):
    code = "INVALID_AGENT_CONFIG"


def _json_value(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _current(effective: AgentsConfig, agent_name: str) -> dict[str, object]:
    section = getattr(effective, agent_name)
    return {k: _json_value(v) for k, v in section.model_dump().items()}


def _check_type(field: str, current: object, value: object) -> None:
    ok = (
        isinstance(value, bool)
        if isinstance(current, bool)
        else isinstance(value, int) and not isinstance(value, bool)
        if isinstance(current, int)
        else isinstance(value, list) and all(isinstance(v, str) for v in value)
        if isinstance(current, list)
        else False
    )
    if not ok:
        raise ProposalInvalid(f"{field}: expected the same type as the current value")


def propose_agent_config(
    session: Session,
    *,
    agent_name: str,
    changes: Mapping[str, Any],
    rationale: str,
    proposed_by: str,
    effective: AgentsConfig,
    now: datetime,
    source: str = "OPERATOR",
    evidence: Mapping[str, Any] | None = None,
) -> AgentConfigProposal:
    if agent_name in LOCKED_SCOPES:
        raise ProposalInvalid(f"{agent_name!r} is locked and cannot be edited here")
    info = AGENT_REGISTRY.get(agent_name)
    if info is None:
        raise ProposalNotFound(f"unknown agent {agent_name!r}")
    if source not in SOURCES:
        raise ProposalInvalid(f"unknown source {source!r}")
    if not changes:
        raise ProposalInvalid("changes must not be empty")
    unknown = sorted(set(changes) - set(info.editable))
    if unknown:
        raise ProposalInvalid(f"not editable for {agent_name}: {unknown}")
    current = _current(effective, agent_name)
    for field, value in changes.items():
        _check_type(field, current[field], value)
    if all(current[f] == v for f, v in changes.items()):
        raise ProposalInvalid("no change: every value equals the current setting")
    candidate = {**effective.model_dump(), agent_name: {**current, **changes}}
    try:
        AgentsConfig.model_validate(candidate)
    except ValidationError as exc:
        raise ProposalInvalid(str(exc)) from exc

    for older in session.scalars(
        select(AgentConfigProposal).where(
            AgentConfigProposal.agent_name == agent_name,
            AgentConfigProposal.status == PENDING,
            AgentConfigProposal.proposed_by == proposed_by,  # never discard someone else's
        )
    ):
        older.status = SUPERSEDED
    row = AgentConfigProposal(
        id=uuid4(),
        agent_name=agent_name,
        changes_json=dict(changes),
        base_json={f: current[f] for f in changes},
        rationale=rationale,
        status=PENDING,
        source=source,
        evidence_json=dict(evidence) if evidence is not None else None,
        proposed_by=proposed_by,
        created_at=now,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        actor=proposed_by,
        action="agent_config.propose",
        target=f"agent:{agent_name}",
        request_id=None,
        before=row.base_json,
        after=row.changes_json,
        result=PENDING,
        now=now,
        details={"proposal_id": str(row.id), "source": source},
    )
    return row


def review_agent_config(
    session: Session,
    proposal_id: UUID,
    *,
    approve: bool,
    reviewer: str,
    now: datetime,
    effective: AgentsConfig,
) -> AgentConfigProposal:
    row = session.scalar(
        select(AgentConfigProposal).where(AgentConfigProposal.id == proposal_id).with_for_update()
    )
    if row is None:
        raise ProposalNotFound(f"unknown proposal {proposal_id}")
    if row.status != PENDING:
        raise ProposalConflict(f"proposal is {row.status}; only PENDING_REVIEW can be reviewed")
    if approve:
        current = _current(effective, row.agent_name)
        if any(current.get(f) != v for f, v in row.base_json.items()):
            raise ProposalConflict("stale: the setting changed since this proposal was written")
        try:  # two proposals can each be valid alone and invalid together
            AgentsConfig.model_validate(
                {**effective.model_dump(), row.agent_name: {**current, **row.changes_json}}
            )
        except ValidationError as exc:
            raise ProposalConflict(f"combined settings would be invalid: {exc}") from exc
    row.status = APPROVED if approve else REJECTED
    row.reviewed_by = reviewer
    row.reviewed_at = now
    session.flush()
    record_audit(
        session,
        actor=reviewer,
        action="agent_config.approve" if approve else "agent_config.reject",
        target=f"agent:{row.agent_name}",
        request_id=None,
        before={"status": PENDING},
        after={"status": row.status},
        result=row.status,
        now=now,
        details={"proposal_id": str(row.id)},
    )
    return row
