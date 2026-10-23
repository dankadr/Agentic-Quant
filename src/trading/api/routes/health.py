"""Liveness and readiness. Neither requires authentication and neither exposes sensitive data."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
from trading.data.calendar import CalendarUnavailable, MarketCalendar
from trading.db.models.market import MarketBar
from trading.jobs.lease import EXECUTOR_LEASE, current_lease

router = APIRouter(prefix="/v1/health", tags=["health"])


@dataclass(frozen=True)
class Readiness:
    database: Literal["ok", "error"]
    market_data: str
    executor_lease: Literal["held", "missing"]

    @property
    def ready(self) -> bool:
        return self.database == "ok" and self.executor_lease == "held"


MARKET_DATA_STALE_AFTER = timedelta(seconds=120)


def _market_data_state(session: Session, clock: Clock, calendar: MarketCalendar | None) -> str:
    """not_configured (nothing ever ingested) | closed | stale | ok."""
    if calendar is None:
        return "not_configured"
    last = session.scalar(select(func.max(MarketBar.ingested_at)))
    if last is None:
        return "not_configured"
    now = clock.now()
    try:
        in_session = calendar.session_at(now) is not None
    except CalendarUnavailable:
        return "stale"  # cannot tell whether the market is open: fail closed
    if not in_session:
        return "closed"
    return "stale" if now - last > MARKET_DATA_STALE_AFTER else "ok"


def compute_readiness(
    session_factory: sessionmaker[Session],
    clock: Clock,
    calendar: MarketCalendar | None = None,
) -> Readiness:
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            lease = current_lease(session, EXECUTOR_LEASE, now=clock.now())
            market_data = _market_data_state(session, clock, calendar)
    except SQLAlchemyError:
        return Readiness("error", "not_configured", "missing")
    return Readiness("ok", market_data, "held" if lease is not None else "missing")


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    state = compute_readiness(
        request.app.state.session_factory, request.app.state.clock, request.app.state.calendar
    )
    body = {
        "status": "ready" if state.ready else "not_ready",
        "database": state.database,
        "market_data": state.market_data,
        "executor_lease": state.executor_lease,
    }
    return JSONResponse(body, status_code=200 if state.ready else 503)
