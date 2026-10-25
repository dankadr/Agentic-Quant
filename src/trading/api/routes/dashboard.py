"""Read-only data for the local dashboard, plus the dashboard's static files.

The page itself carries no data and no secrets. It asks the user for an API key (kept in
sessionStorage) and calls the same authenticated /v1 endpoints as any other client.
"""

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
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
from trading.db.models.research import AgentRun, Experiment
from trading.monitoring.report import daily_report

router = APIRouter(tags=["dashboard"])
STATIC = Path(__file__).resolve().parent.parent / "static"
_MEDIA = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}  # fmt: skip
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_HEADERS = {
    "Content-Security-Policy": _CSP,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _static(name: str) -> Response:
    """Serve one file from the static directory by exact, whitelisted name (no path tricks)."""
    path = STATIC / name
    if not _NAME.match(name) or path.suffix not in _MEDIA or not path.is_file():
        raise ApiProblem(404, "NOT_FOUND", "No such dashboard file")
    return FileResponse(path, media_type=_MEDIA[path.suffix], headers=_HEADERS)


@router.get("/dashboard", include_in_schema=False)
def dashboard_page() -> Response:
    return _static("index.html")


@router.get("/dashboard/{name}", include_in_schema=False)
def dashboard_file(name: str) -> Response:
    return _static(name)


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


class SymbolCoverage(BaseModel):
    symbol: str
    bars: list[int]
    flagged: list[int]


class CoverageOut(BaseModel):
    bucket_minutes: int
    buckets: int
    expected_per_bucket: int
    provider: str
    symbols: list[SymbolCoverage]


COVERAGE_BUCKET = timedelta(minutes=30)


@router.get("/v1/data/coverage", response_model=CoverageOut)
def get_data_coverage(
    request: Request,
    day: date = Query(alias="date"),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    config: AppConfig = Depends(get_config),
) -> CoverageOut:
    """Bars received per symbol per half hour of the session (to spot gaps at a glance)."""
    provider = (
        session.execute(text("SELECT provider FROM market_bars ORDER BY ts DESC LIMIT 1")).scalar()
        or "replay"
    )
    calendar = request.app.state.calendar
    info = calendar.session_on(day) if calendar is not None else None
    minutes = int(COVERAGE_BUCKET.total_seconds() // 60)
    if info is None:
        return CoverageOut(
            bucket_minutes=minutes,
            buckets=0,
            expected_per_bucket=minutes,
            provider=provider,
            symbols=[],
        )
    span = int((info.close_at - info.open_at).total_seconds())
    buckets = max(1, -(-span // int(COVERAGE_BUCKET.total_seconds())))  # ceil
    grid = {u.symbol: ([0] * buckets, [0] * buckets) for u in config.universe}
    rows = session.execute(
        text(
            "SELECT i.symbol, floor(extract(epoch FROM (b.ts - :open)) / :secs)::int AS bucket,"
            " count(*) AS n, count(*) FILTER (WHERE cardinality(b.quality_flags) > 0) AS flagged"
            " FROM market_bars b JOIN instruments i ON i.id = b.instrument_id"
            " WHERE b.provider = :prov AND b.ts >= :open AND b.ts < :close GROUP BY 1, 2"
        ),
        {
            "open": info.open_at,
            "close": info.close_at,
            "secs": int(COVERAGE_BUCKET.total_seconds()),
            "prov": provider,
        },
    )
    for r in rows:
        if r.symbol in grid and 0 <= r.bucket < buckets:
            grid[r.symbol][0][r.bucket] = r.n
            grid[r.symbol][1][r.bucket] = r.flagged
    return CoverageOut(
        bucket_minutes=minutes,
        buckets=buckets,
        expected_per_bucket=minutes,
        provider=provider,
        symbols=[SymbolCoverage(symbol=k, bars=v[0], flagged=v[1]) for k, v in grid.items()],
    )


class ExperimentOut(BaseModel):
    id: str
    kind: str
    strategy_id: str | None
    hypothesis: str
    status: str
    params: dict[str, Any]
    data_range: dict[str, Any]
    metrics: dict[str, Any]
    notes: str
    created_at: UtcDatetime


class ExperimentList(BaseModel):
    items: list[ExperimentOut]


@router.get("/v1/research/experiments", response_model=ExperimentList)
def get_experiments(
    limit: int = Query(default=30, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> ExperimentList:
    rows = session.scalars(
        select(Experiment).order_by(Experiment.created_at.desc(), Experiment.id.desc()).limit(limit)
    )
    return ExperimentList(
        items=[
            ExperimentOut(
                id=str(r.id),
                kind=r.kind,
                strategy_id=r.strategy_id,
                hypothesis=r.hypothesis,
                status=r.status,
                params=dict(r.params_json),
                data_range=dict(r.data_range_json),
                metrics=dict(r.metrics_json),
                notes=r.notes,
                created_at=r.created_at,
            )
            for r in rows
        ]
    )
