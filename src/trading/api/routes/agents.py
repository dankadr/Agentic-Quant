"""Agent outcomes: status, timing, model identity, token usage, and why a run was skipped or failed.

Read-only. A run's ``cost_usd`` is ``null`` when unknown; a self-hosted endpoint has no metered
cost, and that must not be shown as zero.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_session
from trading.core.time import UtcDatetime
from trading.db.models.research import AgentRun

router = APIRouter(prefix="/v1/agent-runs", tags=["agents"])


class AgentRunOut(BaseModel):
    id: str
    agent_name: str
    status: str
    model: str
    prompt_version: str
    started_at: UtcDatetime
    completed_at: UtcDatetime | None
    available_at: UtcDatetime | None
    attempt: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: str | None
    skip_reason: str | None
    error: str | None


class AgentRunList(BaseModel):
    items: list[AgentRunOut]


def _out(run: AgentRun) -> AgentRunOut:
    result: dict[str, Any] = run.result_json or {}
    return AgentRunOut(
        id=str(run.id),
        agent_name=run.agent_name,
        status=run.status,
        model=run.model,
        prompt_version=run.prompt_version,
        started_at=run.started_at,
        completed_at=run.completed_at,
        available_at=run.available_at,
        attempt=run.attempt,
        prompt_tokens=run.prompt_tokens,
        completion_tokens=run.completion_tokens,
        cost_usd=None if run.cost_usd is None else str(run.cost_usd),
        skip_reason=result.get("skip_reason"),
        error=result.get("error"),
    )


@router.get("", response_model=AgentRunList)
def list_agent_runs(
    agent_name: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> AgentRunList:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc(), AgentRun.id).limit(limit)
    if agent_name is not None:
        stmt = stmt.where(AgentRun.agent_name == agent_name)
    return AgentRunList(items=[_out(run) for run in session.scalars(stmt)])
