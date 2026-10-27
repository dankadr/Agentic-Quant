"""Read-only views of agent runs for the dashboard: per-agent numbers and per-decision "why".

Everything in ``result_json`` is model output or derived from it, so it is treated as untrusted:
missing or oddly typed keys never raise, and nothing here is rendered as HTML.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.db.models.research import AgentRun
from trading.db.models.trading import OrderIntent

Health = Literal["ok", "degraded", "failing", "idle"]
_BAD = ("REJECTED_INVALID", "FAILED")
MACRO_STRATEGIES = ("orb_v1", "vwap_v1")


@dataclass(frozen=True)
class AgentSummary:
    name: str
    runs: int
    completed: int
    rejected: int
    failed: int
    prompt_tokens: int
    completion_tokens: int
    last_run_at: datetime | None
    last_error: str | None
    health: Health


@dataclass(frozen=True)
class Decision:
    run_id: UUID
    at: datetime
    status: str
    headline: str
    confidence: Decimal | None
    evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    error: str | None
    input_hash: str | None
    affected_orders: tuple[str, ...]


def _result(run: AgentRun) -> dict[str, Any]:
    return run.result_json if isinstance(run.result_json, dict) else {}


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _strings(value: object) -> tuple[str, ...]:
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _when(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def agent_summary(session: Session, name: str, *, since: datetime) -> AgentSummary:
    runs = list(
        session.scalars(
            select(AgentRun)
            .where(AgentRun.agent_name == name, AgentRun.started_at >= since)
            .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
        )
    )
    if not runs:
        return AgentSummary(name, 0, 0, 0, 0, 0, 0, None, None, "idle")
    tokens = [_result(r).get("tokens") for r in runs]
    prompt = sum(_int(t.get("prompt")) for t in tokens if isinstance(t, dict))
    completion = sum(_int(t.get("completion")) for t in tokens if isinstance(t, dict))
    latest = runs[0]
    health: Health = (
        "failing"
        if latest.status in _BAD
        else "degraded"
        if any(r.status != "COMPLETED" for r in runs)
        else "ok"
    )
    return AgentSummary(
        name=name,
        runs=len(runs),
        completed=sum(r.status == "COMPLETED" for r in runs),
        rejected=sum(r.status == "REJECTED_INVALID" for r in runs),
        failed=sum(r.status == "FAILED" for r in runs),
        prompt_tokens=prompt,
        completion_tokens=completion,
        last_run_at=latest.started_at,
        last_error=next((e for r in runs if (e := _text(_result(r).get("error")))), None),
        health=health,
    )


def _affected(session: Session, run: AgentRun, strategies: Sequence[str]) -> tuple[str, ...]:
    until = _when(_result(run).get("valid_until"))
    if until is None:
        return ()
    intents = session.scalars(
        select(OrderIntent)
        .where(
            OrderIntent.is_entry.is_(True),
            OrderIntent.created_at >= run.started_at,
            OrderIntent.created_at < until,
        )
        .order_by(OrderIntent.created_at)
    )
    return tuple(i.client_order_id for i in intents if set(i.strategy_ids) & set(strategies))


def agent_decisions(
    session: Session,
    name: str,
    *,
    limit: int = 30,
    macro_strategies: Sequence[str] = MACRO_STRATEGIES,
) -> list[Decision]:
    runs = session.scalars(
        select(AgentRun)
        .where(AgentRun.agent_name == name)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
        .limit(limit)
    )
    out: list[Decision] = []
    for run in runs:
        result = _result(run)
        error = _text(result.get("error"))
        out.append(
            Decision(
                run_id=run.id,
                at=run.started_at,
                status=run.status,
                headline=_text(result.get("conclusion"))
                or _text(result.get("regime"))
                or error
                or run.status,
                confidence=_decimal(result.get("confidence")),
                evidence=_strings(result.get("evidence")),
                limitations=_strings(result.get("limitations")),
                error=error,
                input_hash=_text(result.get("input_hash")),
                affected_orders=_affected(session, run, macro_strategies)
                if name == "macro"
                else (),
            )
        )
    return out
