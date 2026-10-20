"""Liveness and readiness. Neither requires authentication and neither exposes sensitive data."""

from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import Clock
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


def compute_readiness(session_factory: sessionmaker[Session], clock: Clock) -> Readiness:
    # market_data is reported by the market-data subsystem once it exists (Task 26).
    market_data = "not_configured"
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            lease = current_lease(session, EXECUTOR_LEASE, now=clock.now())
    except SQLAlchemyError:
        return Readiness("error", market_data, "missing")
    return Readiness("ok", market_data, "held" if lease is not None else "missing")


@router.get("/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
def ready(request: Request) -> JSONResponse:
    state = compute_readiness(request.app.state.session_factory, request.app.state.clock)
    body = {
        "status": "ready" if state.ready else "not_ready",
        "database": state.database,
        "market_data": state.market_data,
        "executor_lease": state.executor_lease,
    }
    return JSONResponse(body, status_code=200 if state.ready else 503)
