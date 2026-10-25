"""Read-only data for the local dashboard, plus the dashboard's static files.

The page itself carries no data and no secrets. It asks the user for an API key (kept in
sessionStorage) and calls the same authenticated /v1 endpoints as any other client.
"""

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_config, get_session
from trading.api.problems import ApiProblem
from trading.api.schemas import Money
from trading.core.config import AppConfig
from trading.core.time import UtcDatetime
from trading.db.models.portfolio import Account, EquitySnapshot
from trading.db.models.research import AgentRun
from trading.monitoring.report import daily_report

router = APIRouter(tags=["dashboard"])
STATIC = Path(__file__).resolve().parent.parent / "static"
_FILES = {
    "": ("index.html", "text/html; charset=utf-8"),
    "app.js": ("app.js", "text/javascript; charset=utf-8"),
    "style.css": ("style.css", "text/css; charset=utf-8"),
}
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _static(name: str) -> Response:
    filename, media_type = _FILES[name]
    return FileResponse(
        STATIC / filename,
        media_type=media_type,
        headers={
            "Content-Security-Policy": _CSP,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> Response:
    return _static("")


@router.get("/dashboard/app.js", include_in_schema=False)
def dashboard_js() -> Response:
    return _static("app.js")


@router.get("/dashboard/style.css", include_in_schema=False)
def dashboard_css() -> Response:
    return _static("style.css")


class AgentRunOut(BaseModel):
    id: str
    agent_name: str
    model: str
    prompt_version: str
    status: str
    started_at: UtcDatetime
    result: dict[str, Any] | None


class AgentRunList(BaseModel):
    items: list[AgentRunOut]


@router.get("/v1/agents/runs", response_model=AgentRunList)
def get_agent_runs(
    agent_name: str | None = None,
    limit: int = Query(default=30, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> AgentRunList:
    stmt = select(AgentRun).order_by(AgentRun.started_at.desc(), AgentRun.id.desc()).limit(limit)
    if agent_name is not None:
        stmt = stmt.where(AgentRun.agent_name == agent_name)
    return AgentRunList(
        items=[
            AgentRunOut(
                id=str(r.id),
                agent_name=r.agent_name,
                model=r.model,
                prompt_version=r.prompt_version,
                status=r.status,
                started_at=r.started_at,
                result=r.result_json,
            )
            for r in session.scalars(stmt)
        ]
    )


class EquityPoint(BaseModel):
    ts: UtcDatetime
    kind: str
    equity: Money


class EquityCurve(BaseModel):
    points: list[EquityPoint]


@router.get("/v1/equity", response_model=EquityCurve)
def get_equity(
    day: date = Query(alias="date"),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> EquityCurve:
    from datetime import UTC, datetime, time, timedelta  # noqa: PLC0415

    start = datetime.combine(day, time.min, tzinfo=UTC)
    rows = session.scalars(
        select(EquitySnapshot)
        .join(Account, Account.id == EquitySnapshot.account_id)
        .where(
            Account.name.not_like("replay-%"),
            Account.name.not_like("backtest%"),
            EquitySnapshot.ts >= start,
            EquitySnapshot.ts < start + timedelta(days=1),
        )
        .order_by(EquitySnapshot.ts)
    )
    return EquityCurve(points=[EquityPoint(ts=r.ts, kind=r.kind, equity=r.equity) for r in rows])


@router.get("/v1/report/daily")
def get_daily_report(
    day: date = Query(alias="date"),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    config: AppConfig = Depends(get_config),
) -> dict[str, Any]:
    provider = (
        session.execute(text("SELECT provider FROM market_bars ORDER BY ts DESC LIMIT 1")).scalar()
        or "replay"
    )
    body = daily_report(session, day, config=config, provider=provider)
    if "error" in body:
        raise ApiProblem(503, "STATE_UNAVAILABLE", "Report unavailable", detail=str(body["error"]))
    return body
