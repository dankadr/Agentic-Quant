"""Operator controls: pause and resume paper trading. Authenticated, audited, idempotent."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_operator, require_viewer
from trading.api.deps import get_clock, get_session
from trading.api.idempotency import run_idempotent
from trading.api.problems import ApiProblem
from trading.api.routes.health import compute_readiness
from trading.control.commands import (
    CommandKind,
    CommandNotAllowed,
    CommandNotFound,
    CommandView,
    get_command,
    request_command,
    resume_refusal,
)
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
    """Always allowed: pausing is the safe direction. It abandons an in-progress flatten."""
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
    """Only after readiness checks pass (DB reachable, executor lease held, risk state NORMAL) and
    no operator command is still running."""

    def action() -> tuple[int, dict[str, Any]]:
        state = get_system_state(session, for_update=True)  # no command can start from here on
        readiness = compute_readiness(
            request.app.state.session_factory, clock, request.app.state.calendar
        )
        risk_state = state.risk_state
        checks = {
            "database": readiness.database,
            "executor_lease": readiness.executor_lease,
            "market_data": readiness.market_data,
            "risk_state": risk_state.value,
        }
        data_usable = readiness.market_data not in ("stale", "incomplete")
        if not (readiness.ready and risk_state is RiskState.NORMAL and data_usable):
            raise ApiProblem(
                503, "NOT_READY", "System is not ready to resume", extra={"checks": checks}
            )
        refusal = resume_refusal(session, state)
        if refusal is not None:
            raise ApiProblem(
                409,
                "COMMAND_IN_PROGRESS",
                refusal.reason,
                extra={
                    "open_commands": [
                        {"command_id": str(c.id), "kind": c.kind.value, "status": c.status.value}
                        for c in refusal.open_commands
                    ]
                },
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


class CommandResponse(BaseModel):
    command_id: str
    kind: str
    status: str
    state: str
    version: int
    progress: dict[str, Any] = Field(default_factory=dict)


def _command_action(
    session: Session,
    request: Request,
    principal: AuthPrincipal,
    clock: Clock,
    body: ControlRequest,
    kind: CommandKind,
) -> tuple[int, dict[str, Any]]:
    try:
        view, state = request_command(
            session,
            kind=kind,
            actor=principal.name,
            reason=body.reason,
            expected_version=body.expected_version,
            request_id=getattr(request.state, "request_id", None),
            now=clock.now(),
        )
    except CommandNotAllowed as exc:
        raise ApiProblem(409, "COMMAND_NOT_ALLOWED", str(exc)) from exc
    return 202, {
        "command_id": str(view.id),
        "kind": view.kind.value,
        "status": view.status.value,
        "state": state.trading_state.value,
        "version": state.version,
        "progress": view.progress,
    }


def _run_command(
    kind: CommandKind,
    route: str,
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None,
    principal: AuthPrincipal,
    session: Session,
    clock: Clock,
) -> JSONResponse:
    return run_idempotent(
        session,
        principal=principal,
        route=route,
        key=idempotency_key,
        body=body,
        action=lambda: _command_action(session, request, principal, clock, body, kind),
        now=clock.now(),
    )


@router.post("/stop-entries", status_code=202, responses={202: {"model": CommandResponse}})
def stop_entries(
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """No new entries. Entry orders are cancelled; protective exits keep running."""
    return _run_command(
        CommandKind.STOP_ENTRIES,
        "POST /v1/control/stop-entries",
        body,
        request,
        idempotency_key,
        principal,
        session,
        clock,
    )


@router.post("/cancel-open-orders", status_code=202, responses={202: {"model": CommandResponse}})
def cancel_open_orders(
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """No new entries, and every order open at this moment is cancelled. Exits placed afterwards
    keep running. Positions are left alone."""
    return _run_command(
        CommandKind.CANCEL_OPEN_ORDERS,
        "POST /v1/control/cancel-open-orders",
        body,
        request,
        idempotency_key,
        principal,
        session,
        clock,
    )


@router.post("/flatten", status_code=202, responses={202: {"model": CommandResponse}})
def flatten(
    body: ControlRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    principal: AuthPrincipal = Depends(require_operator),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> JSONResponse:
    """Cancel orders and liquidate every position. Never enables entries; ends in EXIT_ONLY."""
    return _run_command(
        CommandKind.FLATTEN,
        "POST /v1/control/flatten",
        body,
        request,
        idempotency_key,
        principal,
        session,
        clock,
    )


@router.get("/commands/{command_id}", response_model=CommandResponse)
def command_status(
    command_id: UUID,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> CommandResponse:
    """Progress of an operator command: 404 if unknown, otherwise its status and progress."""
    try:
        view: CommandView = get_command(session, command_id)
    except CommandNotFound as exc:
        raise ApiProblem(404, "NOT_FOUND", str(exc)) from exc
    state = get_system_state(session)
    return CommandResponse(
        command_id=str(view.id),
        kind=view.kind.value,
        status=view.status.value,
        state=state.trading_state.value,
        version=state.version,
        progress=view.progress,
    )
