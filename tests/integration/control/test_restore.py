from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from trading.control.state import (
    RiskState,
    TradingState,
    get_system_state,
    mark_restored,
    set_risk_state,
)

NOW = datetime(2026, 10, 28, 16, 0, tzinfo=UTC)


def test_a_restored_instance_is_paused_and_must_reconcile_before_re_arming(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        set_risk_state(s, risk_state=RiskState.NORMAL, reason="before backup", actor="t", now=NOW)
    with session_factory.begin() as s:
        state = mark_restored(s, reference="drill-42", now=NOW)
    assert state.trading_state is TradingState.PAUSED
    assert state.risk_state is RiskState.RECONCILIATION_REQUIRED
    assert state.risk_reason == "restored: drill-42"
    with session_factory() as s:
        assert get_system_state(s).trading_state is TradingState.PAUSED
