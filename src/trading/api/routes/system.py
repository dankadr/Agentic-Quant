"""System status."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_clock, get_session
from trading.control.state import get_system_state
from trading.core.clock import Clock
from trading.core.time import UtcDatetime, isoformat_z
from trading.data.calendar import NEW_YORK, CalendarUnavailable, MarketCalendar
from trading.db.models.market import MarketBar

router = APIRouter(prefix="/v1/system", tags=["system"])


class ReleaseStatus(BaseModel):
    revision: str
    image: str
    config_sha256: str
    schema_head: str | None  # newest migration shipped with this code
    schema_applied: str | None  # migration the database actually has


class SystemStatus(BaseModel):
    mode: str
    trading_enabled: bool
    trading_state: str
    risk_state: str
    risk_reason: str | None
    version: int
    session: dict[str, object] | None
    last_data_at: UtcDatetime | None
    last_reconciled_at: UtcDatetime | None
    release: ReleaseStatus


def _session_info(request: Request, clock: Clock) -> dict[str, object] | None:
    calendar: MarketCalendar | None = request.app.state.calendar
    if calendar is None:
        return None
    now = clock.now()
    try:
        info = calendar.session_on(now.astimezone(NEW_YORK).date())
    except CalendarUnavailable:
        return None
    if info is None:
        return None
    return {
        "date": info.session_date.isoformat(),
        "open_at": isoformat_z(info.open_at),
        "close_at": isoformat_z(info.close_at),
        "is_open": info.open_at <= now < info.close_at,
        "is_half_day": info.is_half_day,
    }


@router.get("/status", response_model=SystemStatus)
def system_status(
    request: Request,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> SystemStatus:
    state = get_system_state(session)
    release = request.app.state.release
    return SystemStatus(
        mode=state.mode,
        trading_enabled=state.trading_enabled,
        trading_state=state.trading_state.value,
        risk_state=state.risk_state.value,
        risk_reason=state.risk_reason,
        version=state.version,
        session=_session_info(request, clock),
        last_data_at=session.scalar(select(func.max(MarketBar.ingested_at))),
        last_reconciled_at=state.last_reconciled_at,
        release=ReleaseStatus(
            revision=release.revision,
            image=release.image,
            config_sha256=release.config_sha256,
            schema_head=release.schema_head,
            schema_applied=session.scalar(
                text(
                    "SELECT CASE WHEN to_regclass('public.alembic_version') IS NULL THEN NULL"
                    " ELSE (SELECT version_num FROM alembic_version LIMIT 1) END"
                )
            ),
        ),
    )
