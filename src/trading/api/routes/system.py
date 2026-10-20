"""System status."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_session
from trading.control.state import get_system_state
from trading.core.time import UtcDatetime

router = APIRouter(prefix="/v1/system", tags=["system"])


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


@router.get("/status", response_model=SystemStatus)
def system_status(
    _: AuthPrincipal = Depends(require_viewer), session: Session = Depends(get_session)
) -> SystemStatus:
    state = get_system_state(session)
    return SystemStatus(
        mode=state.mode,
        trading_enabled=state.trading_enabled,
        trading_state=state.trading_state.value,
        risk_state=state.risk_state.value,
        risk_reason=state.risk_reason,
        version=state.version,
        session=None,
        last_data_at=None,
        last_reconciled_at=state.last_reconciled_at,
    )
