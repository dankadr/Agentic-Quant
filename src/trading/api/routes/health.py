"""Liveness, readiness, and trading readiness.

Liveness asks only whether the process answers. Readiness asks whether the infrastructure works.
Trading readiness asks whether NEW ENTRIES are permitted right now, and why not. Neither of the
first two requires authentication or exposes sensitive data; trading readiness needs a viewer.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.api.auth import AuthPrincipal, require_viewer
from trading.control.state import RiskState, TradingState, get_system_state
from trading.core.clock import Clock
from trading.data.calendar import CalendarUnavailable, MarketCalendar
from trading.db.models.market import Instrument, MarketBar
from trading.jobs.lease import EXECUTOR_LEASE, current_lease

router = APIRouter(prefix="/v1/health", tags=["health"])

MARKET_DATA_STALE_AFTER = timedelta(seconds=120)
BAR_LENGTH = timedelta(minutes=1)  # bar timestamps are bar STARTS (Alpaca's ``t``)


@dataclass(frozen=True)
class Readiness:
    database: Literal["ok", "error"]
    market_data: str
    executor_lease: Literal["held", "missing"]

    @property
    def ready(self) -> bool:
        return self.database == "ok" and self.executor_lease == "held"


def _market_data_state(session: Session, clock: Clock, calendar: MarketCalendar | None) -> str:
    """not_configured | closed | stale | incomplete | ok.

    Freshness is judged per instrument from the observation time of its latest closed bar, not
    from when this system ingested it: an old bar ingested just now is still old.
    ``incomplete`` means some configured instrument has no bars at all.
    """
    if calendar is None:
        return "not_configured"
    latest = dict(
        session.execute(
            select(MarketBar.instrument_id, func.max(MarketBar.ts)).group_by(
                MarketBar.instrument_id
            )
        ).all()
    )
    if not latest:
        return "not_configured"
    now = clock.now()
    try:
        in_session = calendar.session_at(now) is not None
    except CalendarUnavailable:
        return "stale"  # cannot tell whether the market is open: fail closed
    if not in_session:
        return "closed"
    universe = session.scalar(select(func.count()).select_from(Instrument)) or 0
    if len(latest) < universe:
        return "incomplete"
    oldest_close: datetime = min(ts for ts in latest.values()) + BAR_LENGTH
    return "stale" if now - oldest_close > MARKET_DATA_STALE_AFTER else "ok"


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


@dataclass(frozen=True)
class TradingReadiness:
    entries_permitted: bool
    exits_permitted: bool
    reasons: list[str] = field(default_factory=list)


def compute_trading_readiness(
    session_factory: sessionmaker[Session],
    clock: Clock,
    calendar: MarketCalendar | None,
) -> TradingReadiness:
    """Entries need every precondition. Exits need only the ones that protect a position."""
    readiness = compute_readiness(session_factory, clock, calendar)
    if readiness.database != "ok":
        return TradingReadiness(False, False, ["DATABASE_UNAVAILABLE"])
    if readiness.executor_lease != "held":
        return TradingReadiness(False, False, ["EXECUTOR_LEASE_MISSING"])
    with session_factory() as session:
        state = get_system_state(session)

    reasons: list[str] = []
    if state.trading_state is TradingState.PAUSED:
        reasons.append("TRADING_PAUSED")
    elif state.trading_state is not TradingState.PAPER_RUNNING:
        reasons.append(f"TRADING_{state.trading_state.value}")
    if state.risk_state is not RiskState.NORMAL:
        reasons.append(f"RISK_{state.risk_state.value}")
    if readiness.market_data == "closed":
        reasons.append("MARKET_CLOSED")  # a normal state, not a failure
    elif readiness.market_data != "ok":
        reasons.append(f"MARKET_DATA_{readiness.market_data.upper()}")

    entries = (
        state.trading_state is TradingState.PAPER_RUNNING
        and state.risk_state is RiskState.NORMAL
        and readiness.market_data == "ok"
    )
    exits = state.trading_state is not TradingState.PAUSED and state.risk_state not in (
        RiskState.UNKNOWN,
        RiskState.RECONCILIATION_REQUIRED,
    )
    return TradingReadiness(entries, exits, reasons)


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


@router.get("/trading")
def trading(request: Request, _: AuthPrincipal = Depends(require_viewer)) -> dict[str, object]:
    """Whether new entries are permitted now, whether exits are, and every reason they are not."""
    state = compute_trading_readiness(
        request.app.state.session_factory, request.app.state.clock, request.app.state.calendar
    )
    return {
        "entries_permitted": state.entries_permitted,
        "exits_permitted": state.exits_permitted,
        "reasons": state.reasons,
    }
