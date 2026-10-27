"""Agents: numbers, rules, per-decision "why", reviewed setting edits and suggestions.

Reads are for viewers; proposing and reviewing an edit need the operator role, are idempotent and
audited. Nothing here changes a running process: approved edits apply when no session is open.
Model output (evidence, headlines, errors) is returned as plain strings and rendered as text.
"""

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.agents.config_proposals import (
    APPROVED,
    ProposalConflict,
    ProposalInvalid,
    ProposalNotFound,
    propose_agent_config,
    review_agent_config,
)
from trading.agents.effective_config import overlay_agents
from trading.agents.registry import AGENT_REGISTRY, LOCKED_SCOPES, AgentInfo
from trading.agents.stats import agent_decisions, agent_summary
from trading.agents.suggestions import suggest
from trading.api.auth import AuthPrincipal, require_operator, require_viewer
from trading.api.deps import get_clock, get_session
from trading.api.idempotency import run_idempotent
from trading.api.problems import ApiProblem
from trading.core.clock import Clock
from trading.core.config import AgentsConfig
from trading.core.time import UtcDatetime
from trading.db.models.research import AgentConfigProposal

router = APIRouter(tags=["agents"])
WINDOW = timedelta(hours=24)


class AgentListItem(BaseModel):
    name: str
    health: str
    last_run_at: UtcDatetime | None
    enabled: bool
    uses_llm: bool


class AgentList(BaseModel):
    items: list[AgentListItem]


class AgentInfoOut(BaseModel):
    name: str
    job: str
    can_affect: list[str]
    cannot: list[str]
    uses_llm: bool
    prompt_version: str
    prompt_text: str | None
    valid_output: str
    on_failure: str


class SummaryOut(BaseModel):
    runs: int
    completed: int
    rejected: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    last_run_at: UtcDatetime | None
    last_error: str | None
    health: str


class ConfigField(BaseModel):
    name: str
    value: Any
    pending: Any | None


class ProposalOut(BaseModel):
    id: str
    agent_name: str
    changes: dict[str, Any]
    base: dict[str, Any]
    rationale: str
    status: str
    source: str
    proposed_by: str
    created_at: UtcDatetime
    reviewed_by: str | None
    reviewed_at: UtcDatetime | None
    applied_at: UtcDatetime | None


class ProposalList(BaseModel):
    items: list[ProposalOut]


class AgentDetail(BaseModel):
    info: AgentInfoOut
    summary: SummaryOut
    config: list[ConfigField]
    locked: list[str]
    proposals: list[ProposalOut]


class DecisionOut(BaseModel):
    run_id: str
    at: UtcDatetime
    status: str
    headline: str
    confidence: str | None
    evidence: list[str]
    limitations: list[str]
    error: str | None
    input_hash: str | None
    affected_orders: list[str]


class DecisionList(BaseModel):
    items: list[DecisionOut]


class SuggestionOut(BaseModel):
    agent: str
    title: str
    field: str | None
    current: Any | None
    proposed: Any | None
    evidence_n: int
    evidence_needed: int
    status: str
    rationale: str


class SuggestionList(BaseModel):
    items: list[SuggestionOut]


class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: dict[str, Any]
    rationale: str = Field(min_length=1, max_length=500)
    source: Literal["OPERATOR", "SUGGESTION"] = "OPERATOR"


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approve: bool


def _info(name: str) -> AgentInfo:
    info = AGENT_REGISTRY.get(name)
    if info is None:
        raise ApiProblem(404, "NOT_FOUND", f"unknown agent {name!r}")
    return info


def _json(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value


def _proposal_out(row: AgentConfigProposal) -> ProposalOut:
    return ProposalOut(
        id=str(row.id),
        agent_name=row.agent_name,
        changes=row.changes_json,
        base=row.base_json,
        rationale=row.rationale,
        status=row.status,
        source=row.source,
        proposed_by=row.proposed_by,
        created_at=row.created_at,
        reviewed_by=row.reviewed_by,
        reviewed_at=row.reviewed_at,
        applied_at=row.applied_at,
    )


def get_base(request: Request) -> AgentsConfig:
    base: AgentsConfig = request.app.state.yaml_agents
    return base


def _running(base: AgentsConfig, session: Session) -> AgentsConfig:
    """What the services run with: the YAML defaults plus applied edits."""
    return overlay_agents(base, session, ("APPLIED",))


def _projected(base: AgentsConfig, session: Session) -> AgentsConfig:
    """What will run after the next session boundary: applied plus approved edits."""
    return overlay_agents(base, session, ("APPLIED", APPROVED))


def _since(clock: Clock) -> datetime:
    return clock.now() - WINDOW


@router.get("/v1/agents", response_model=AgentList)
def list_agents(
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
    clock: Clock = Depends(get_clock),
) -> AgentList:
    running = _running(base, session)
    items = []
    for name, info in AGENT_REGISTRY.items():
        summary = agent_summary(session, name, since=_since(clock))
        items.append(
            AgentListItem(
                name=name,
                health=summary.health,
                last_run_at=summary.last_run_at,
                enabled=bool(getattr(running, name).enabled),
                uses_llm=info.uses_llm,
            )
        )
    return AgentList(items=items)


@router.get("/v1/agents/{name}", response_model=AgentDetail)
def get_agent(
    name: str,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
    clock: Clock = Depends(get_clock),
) -> AgentDetail:
    info = _info(name)
    summary = agent_summary(session, name, since=_since(clock))
    running = getattr(_running(base, session), name).model_dump()
    projected = getattr(_projected(base, session), name).model_dump()
    fields = [
        ConfigField(
            name=f,
            value=_json(running[f]),
            pending=_json(projected[f]) if projected[f] != running[f] else None,
        )
        for f in info.editable
    ]
    proposals = session.scalars(
        select(AgentConfigProposal)
        .where(AgentConfigProposal.agent_name == name)
        .order_by(AgentConfigProposal.created_at.desc(), AgentConfigProposal.id.desc())
        .limit(20)
    )
    return AgentDetail(
        info=AgentInfoOut(**{k: v for k, v in asdict(info).items() if k != "editable"}),
        summary=SummaryOut(**{k: v for k, v in asdict(summary).items() if k != "name"}),
        config=fields,
        locked=list(LOCKED_SCOPES),
        proposals=[_proposal_out(p) for p in proposals],
    )


@router.get("/v1/agents/{name}/decisions", response_model=DecisionList)
def get_decisions(
    name: str,
    limit: int = Query(default=30, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
) -> DecisionList:
    _info(name)
    macro_strategies = _running(base, session).macro.dependent_strategies
    decisions = agent_decisions(session, name, limit=limit, macro_strategies=macro_strategies)
    return DecisionList(
        items=[
            DecisionOut(
                run_id=str(d.run_id),
                at=d.at,
                status=d.status,
                headline=d.headline,
                confidence=None if d.confidence is None else str(d.confidence),
                evidence=list(d.evidence),
                limitations=list(d.limitations),
                error=d.error,
                input_hash=d.input_hash,
                affected_orders=list(d.affected_orders),
            )
            for d in decisions
        ]
    )


@router.get("/v1/agents/{name}/suggestions", response_model=SuggestionList)
def get_suggestions(
    name: str,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
    clock: Clock = Depends(get_clock),
) -> SuggestionList:
    _info(name)
    found = suggest(session, name, config=_projected(base, session), now=clock.now())
    return SuggestionList(items=[SuggestionOut(**asdict(s)) for s in found])


@router.get("/v1/agents/{name}/config-proposals", response_model=ProposalList)
def list_proposals(
    name: str,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> ProposalList:
    _info(name)
    rows = session.scalars(
        select(AgentConfigProposal)
        .where(AgentConfigProposal.agent_name == name)
        .order_by(AgentConfigProposal.created_at.desc(), AgentConfigProposal.id.desc())
        .limit(50)
    )
    return ProposalList(items=[_proposal_out(r) for r in rows])


def _problem(exc: Exception) -> ApiProblem:
    if isinstance(exc, ProposalNotFound):
        return ApiProblem(404, "NOT_FOUND", str(exc))
    if isinstance(exc, ProposalConflict):
        return ApiProblem(409, "CONFLICT", str(exc))
    return ApiProblem(422, "INVALID_AGENT_CONFIG", "Invalid agent setting", detail=str(exc))


@router.post("/v1/agents/{name}/config-proposals", responses={201: {"model": ProposalOut}})
def propose(
    name: str,
    body: ProposalRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    def action() -> tuple[int, dict[str, Any]]:
        _info(name)
        projected = _projected(base, session)
        evidence: dict[str, Any] | None = None
        if body.source == "SUGGESTION":
            # Never trust client-supplied evidence: it must match a suggestion proposable now.
            match = next(
                (
                    s
                    for s in suggest(session, name, config=projected, now=clock.now())
                    if s.status == "proposable"
                    and s.field
                    and body.changes == {s.field: s.proposed}
                ),
                None,
            )
            if match is None:
                raise ApiProblem(
                    422,
                    "NO_MATCHING_SUGGESTION",
                    "No proposable suggestion matches these changes",
                )
            evidence = {"evidence_n": match.evidence_n, "rationale": match.rationale}
        try:
            row = propose_agent_config(
                session,
                agent_name=name,
                changes=body.changes,
                rationale=body.rationale,
                proposed_by=principal.name,
                effective=projected,
                now=clock.now(),
                source=body.source,
                evidence=evidence,
            )
        except (ProposalInvalid, ProposalNotFound, ProposalConflict) as exc:
            raise _problem(exc) from exc
        return 201, _proposal_out(row).model_dump(mode="json")

    return run_idempotent(
        session,
        principal=principal,
        route=f"POST /v1/agents/{name}/config-proposals",
        key=idempotency_key,
        body=body,
        action=action,
        now=clock.now(),
    )


@router.post(
    "/v1/agent-config-proposals/{proposal_id}/review", responses={200: {"model": ProposalOut}}
)
def review(
    proposal_id: UUID,
    body: ReviewRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    base: AgentsConfig = Depends(get_base),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    def action() -> tuple[int, dict[str, Any]]:
        try:
            row = review_agent_config(
                session,
                proposal_id,
                approve=body.approve,
                reviewer=principal.name,
                now=clock.now(),
                effective=_projected(base, session),
            )
        except (ProposalInvalid, ProposalNotFound, ProposalConflict) as exc:
            raise _problem(exc) from exc
        return 200, _proposal_out(row).model_dump(mode="json")

    return run_idempotent(
        session,
        principal=principal,
        route=f"POST /v1/agent-config-proposals/{proposal_id}/review",
        key=idempotency_key,
        body=body,
        action=action,
        now=clock.now(),
    )
