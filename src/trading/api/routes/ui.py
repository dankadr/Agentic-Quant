"""Operator dashboard: server-rendered, read-mostly, with CSRF on every state-changing form.

Pages show what an operator needs to decide: is trading permitted and why not, what is open,
what was refused, what the research says and what it could not say, and which incidents are live.
Every value is HTML-escaped by the template engine, so stored text can never run script here.
"""

import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import Response

from trading.alerts.store import acknowledge
from trading.api.auth import hash_api_key
from trading.api.deps import get_clock, get_session
from trading.api.routes.health import compute_trading_readiness
from trading.control.state import get_system_state
from trading.core.clock import Clock
from trading.db.models.documents import ResearchReportRow
from trading.db.models.market import Instrument
from trading.db.models.ops import OperationalCommandRow, Principal
from trading.db.models.ops_alerts import IncidentRow
from trading.db.models.research import AgentRun
from trading.db.models.trading import Order, OrderIntent, RiskDecisionRow
from trading.documents.search import search
from trading.ui.sessions import csrf_ok, lookup, open_session, revoke

router = APIRouter(prefix="/ui", tags=["dashboard"], include_in_schema=False)
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "ui" / "templates"))
LOGIN_COOKIE = "ui_login"
SESSION_COOKIE = "ui_session"


def _secure(request: Request) -> bool:
    settings = request.app.state.settings
    return bool(settings.ui_cookie_secure)


def _page(request: Request, name: str, user: Any, **context: Any) -> Response:
    return TEMPLATES.TemplateResponse(request, name, {"user": user, "active": name, **context})


def _user(request: Request, session: Session, clock: Clock) -> Any:
    return lookup(session, request.cookies.get(SESSION_COOKIE), now=clock.now())


@router.get("/login")
def login_form(request: Request) -> Response:
    token = secrets.token_urlsafe(24)
    response = TEMPLATES.TemplateResponse(
        request, "login.html", {"user": None, "csrf": token, "error": None}
    )
    response.set_cookie(
        LOGIN_COOKIE, token, httponly=True, secure=_secure(request), samesite="strict",
        path="/ui", max_age=600,
    )  # fmt: skip
    return response


@router.post("/login")
def login(
    request: Request,
    api_key: str = Form(max_length=200),
    csrf: str = Form(max_length=200),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    expected = request.cookies.get(LOGIN_COOKIE, "")
    if not csrf_ok(expected, csrf):
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"user": None, "csrf": expected, "error": "Form expired."},
            status_code=403,
        )
    principal = session.scalar(
        select(Principal).where(Principal.api_key_hash == hash_api_key(api_key))
    )
    if principal is None or principal.disabled_at is not None:
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"user": None, "csrf": expected, "error": "Key not accepted."},
            status_code=401,
        )
    token, _ = open_session(session, principal_id=principal.id, now=clock.now())
    response = RedirectResponse("/ui", status_code=303)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, secure=_secure(request), samesite="strict",
        path="/ui", max_age=30 * 60,
    )  # fmt: skip
    response.delete_cookie(LOGIN_COOKIE, path="/ui")
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf: str = Form(max_length=200),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    user = _user(request, session, clock)
    if user is None or not csrf_ok(user.csrf_token, csrf):
        # A forged or stale logout changes nothing: the session stays exactly as it was.
        return Response("Form expired. Reload the page and try again.", status_code=403)
    revoke(session, request.cookies.get(SESSION_COOKIE), now=clock.now())
    response = RedirectResponse("/ui/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/ui")
    return response


def _require(request: Request, session: Session, clock: Clock) -> Any:
    user = _user(request, session, clock)
    if user is None:
        raise _NeedsLogin()
    return user


class _NeedsLogin(Exception):
    pass


@router.get("", response_model=None)
def overview(
    request: Request,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Any:
    try:
        user = _require(request, session, clock)
    except _NeedsLogin:
        return RedirectResponse("/ui/login", status_code=303)
    state = get_system_state(session)
    readiness = compute_trading_readiness(
        request.app.state.session_factory, clock, request.app.state.calendar
    )
    incidents = session.scalars(
        select(IncidentRow).where(IncidentRow.state == "OPEN").order_by(IncidentRow.opened_at)
    ).all()
    return _page(
        request, "overview.html", user,
        state=state, readiness=readiness, release=request.app.state.release,
        incidents=incidents, csrf=user.csrf_token,
    )  # fmt: skip


def _table(request: Request, title: str, session: Session, clock: Clock, rows_fn: Any) -> Any:
    try:
        user = _require(request, session, clock)
    except _NeedsLogin:
        return RedirectResponse("/ui/login", status_code=303)
    headers, rows = rows_fn()
    return _page(request, "table.html", user, title=title, headers=headers, rows=rows)


@router.get("/orders", response_model=None)
def orders(
    request: Request, session: Session = Depends(get_session), clock: Clock = Depends(get_clock)
) -> Any:
    def rows() -> tuple[list[str], list[list[str]]]:
        stmt = (
            select(Order, Instrument.symbol)
            .join(OrderIntent, OrderIntent.id == Order.intent_id)
            .join(Instrument, Instrument.id == Order.instrument_id)
            .order_by(Order.id.desc())
            .limit(100)
        )
        out = [
            [sym, o.side, str(o.qty), str(o.filled_qty), o.status, o.status_reason or ""]
            for o, sym in session.execute(stmt).all()
        ]
        return ["Symbol", "Side", "Qty", "Filled", "Status", "Reason"], out

    return _table(request, "Orders", session, clock, rows)


@router.get("/risk", response_model=None)
def risk(
    request: Request, session: Session = Depends(get_session), clock: Clock = Depends(get_clock)
) -> Any:
    def rows() -> tuple[list[str], list[list[str]]]:
        stmt = (
            select(RiskDecisionRow)
            .where(RiskDecisionRow.allowed.is_(False))
            .order_by(RiskDecisionRow.ts.desc())
            .limit(100)
        )
        out = [
            [
                d.ts.isoformat(),
                str(d.proposal_json.get("strategy_id", "")),
                ", ".join(str(r.get("code", "")) for r in d.reasons_json),
            ]
            for d in session.scalars(stmt)
        ]
        return ["Time", "Strategy", "Refused for"], out

    return _table(request, "Risk denials", session, clock, rows)


@router.get("/agents", response_model=None)
def agents(
    request: Request, session: Session = Depends(get_session), clock: Clock = Depends(get_clock)
) -> Any:
    def rows() -> tuple[list[str], list[list[str]]]:
        stmt = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(100)
        out = [
            [
                r.agent_name,
                r.status,
                r.model,
                r.started_at.isoformat(),
                "" if r.available_at is None else r.available_at.isoformat(),
                "unknown" if r.cost_usd is None else str(r.cost_usd),
            ]
            for r in session.scalars(stmt)
        ]
        return ["Agent", "Status", "Model", "Started", "Available", "Cost"], out

    return _table(request, "Agent runs", session, clock, rows)


@router.get("/commands", response_model=None)
def commands(
    request: Request, session: Session = Depends(get_session), clock: Clock = Depends(get_clock)
) -> Any:
    def rows() -> tuple[list[str], list[list[str]]]:
        stmt = (
            select(OperationalCommandRow)
            .order_by(OperationalCommandRow.requested_at.desc())
            .limit(50)
        )
        out = [
            [c.kind, c.status, c.requested_by, c.reason, str(c.progress_json or {})]
            for c in session.scalars(stmt)
        ]
        return ["Command", "Status", "By", "Reason", "Progress"], out

    return _table(request, "Operational commands", session, clock, rows)


@router.get("/research", response_model=None)
def research(
    request: Request,
    q: str | None = None,
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Any:
    try:
        user = _require(request, session, clock)
    except _NeedsLogin:
        return RedirectResponse("/ui/login", status_code=303)
    query = q or ""
    hits = search(session, query=query, limit=20) if len(query) >= 2 else []
    reports = session.scalars(
        select(ResearchReportRow).order_by(ResearchReportRow.created_at.desc()).limit(30)
    ).all()
    return _page(request, "research.html", user, q=query, hits=hits, reports=reports)


@router.get("/alerts", response_model=None)
def alerts(
    request: Request, session: Session = Depends(get_session), clock: Clock = Depends(get_clock)
) -> Any:
    try:
        user = _require(request, session, clock)
    except _NeedsLogin:
        return RedirectResponse("/ui/login", status_code=303)
    items = session.scalars(
        select(IncidentRow).order_by(IncidentRow.opened_at.desc()).limit(100)
    ).all()
    return _page(request, "alerts.html", user, incidents=items, csrf=user.csrf_token)


@router.post("/alerts/{incident_id}/ack")
def ack(
    request: Request,
    incident_id: UUID,
    csrf: str = Form(max_length=200),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> Response:
    try:
        user = _require(request, session, clock)
    except _NeedsLogin:
        return RedirectResponse("/ui/login", status_code=303)
    if not csrf_ok(user.csrf_token, csrf) or user.principal.role.value != "operator":
        return RedirectResponse("/ui/alerts", status_code=303)
    acknowledge(session, incident_id, by=user.principal.name, now=clock.now())
    return RedirectResponse("/ui/alerts", status_code=303)
