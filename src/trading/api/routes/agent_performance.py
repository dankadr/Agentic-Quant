"""Read-only versioned agent performance evidence; no trading or tuning authority."""

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from trading.agents.effective_config import overlay_agents
from trading.agents.performance import PerformanceReport, performance_report
from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_clock, get_session
from trading.core.clock import Clock

router = APIRouter(tags=["agents"])


@router.get("/v1/agents/performance", response_model=PerformanceReport)
def get_agent_performance(
    request: Request,
    days: int = Query(default=30, ge=1, le=90),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
    clock: Clock = Depends(get_clock),
) -> PerformanceReport:
    now = clock.now()
    agents = overlay_agents(request.app.state.yaml_agents, session, ("APPLIED",))
    return performance_report(
        session, agents=agents, since=now - timedelta(days=days), now=now, days=days
    )
