"""Operator controls: pause and resume paper trading. Authenticated, audited, idempotent."""

from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_operator
from trading.api.deps import get_clock, get_session
from trading.api.idempotency import run_idempotent
from trading.api.problems import ApiProblem
from trading.api.routes.health import compute_readiness
from trading.control.state import RiskState, TradingState, get_system_state, set_trading_state
from trading.core.clock import Clock

router = APIRouter(prefix="/v1/control", tags=["control"])


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=1, max_length=500)
    expected_version: int


class ControlResponse(BaseModel):
    state: str
    event_id: str
    version: int


def _apply(
    session: Session,
    request: Request,
    principal: AuthPrincipal,
    clock: Clock,
    body: ControlRequest,
    target: TradingState,
) -> tuple[int, dict[str, Any]]:
    state, event_id = set_trading_state(
        session,
        target=target,
        expected_version=body.expected_version,
        actor=principal.name,
        reason=body.reason,
        request_id=getattr(request.state, "request_id", None),
        now=clock.now(),
    )
    return 200, {
        "state": state.trading_state.value,
        "event_id": str(event_id),
        "version": state.version,
    }


@router.post("/pause", responses={200: {"model": ControlResponse}})
def pause(
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """Always allowed: pausing is the safe direction."""
    return run_idempotent(
        session,
        principal=principal,
        route="POST /v1/control/pause",
        key=idempotency_key,
        body=body,
        action=lambda: _apply(session, request, principal, clock, body, TradingState.PAUSED),
        now=clock.now(),
    )


@router.post("/resume-paper", responses={200: {"model": ControlResponse}})
def resume_paper(
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """Only after readiness checks pass: DB reachable, executor lease held, risk state NORMAL."""

    def action() -> tuple[int, dict[str, Any]]:
        readiness = compute_readiness(request.app.state.session_factory, clock)
        risk_state = get_system_state(session).risk_state
        checks = {
            "database": readiness.database,
            "executor_lease": readiness.executor_lease,
            "market_data": readiness.market_data,
            "risk_state": risk_state.value,
        }
        if not (
            readiness.ready and risk_state is RiskState.NORMAL and readiness.market_data != "stale"
        ):
            raise ApiProblem(
                503, "NOT_READY", "System is not ready to resume", extra={"checks": checks}
            )
        return _apply(session, request, principal, clock, body, TradingState.PAPER_RUNNING)

    return run_idempotent(
        session,
        principal=principal,
        route="POST /v1/control/resume-paper",
        key=idempotency_key,
        body=body,
        action=action,
        now=clock.now(),
    )
