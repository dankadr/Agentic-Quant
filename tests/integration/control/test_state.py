from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.control.state import (
    RiskState,
    TradingState,
    VersionConflict,
    get_system_state,
    mark_reconciled,
    set_risk_state,
    set_trading_state,
)
from trading.core.clock import ManualClock
from trading.monitoring.audit import list_audit


def _resume(db: Session, clock: ManualClock, version: int = 1, request_id: str | None = None):  # type: ignore[no-untyped-def]
    return set_trading_state(
        db,
        target=TradingState.PAPER_RUNNING,
        expected_version=version,
        actor="op",
        reason="start",
        request_id=request_id,
        now=clock.now(),
    )


def _outbox_types(db: Session) -> list[str]:
    return list(db.execute(text("select event_type from outbox order by created_at, id")).scalars())


def test_initial_state_is_paused_with_unknown_risk(db: Session) -> None:
    state = get_system_state(db)
    assert state.mode == "PAPER_ONLY"
    assert state.trading_state is TradingState.PAUSED
    assert state.risk_state is RiskState.UNKNOWN
    assert state.version == 1
    assert state.trading_enabled is False


def test_resume_bumps_version_and_audits_with_request_id(db: Session, clock: ManualClock) -> None:
    state, event_id = _resume(db, clock, request_id="req-1")
    assert state.trading_state is TradingState.PAPER_RUNNING
    assert state.version == 2
    row = list_audit(db, limit=1, before=None)[0]
    assert row.id == event_id
    assert (row.action, row.result, row.request_id, row.actor) == (
        "trading_state.set",
        "ok",
        "req-1",
        "op",
    )
    assert row.details_json["reason"] == "start"
    assert _outbox_types(db) == ["trading_state_changed"]


def test_pause_after_resume_bumps_again(db: Session, clock: ManualClock) -> None:
    _resume(db, clock)
    state, _ = set_trading_state(
        db,
        target=TradingState.PAUSED,
        expected_version=2,
        actor="op",
        reason="stop",
        request_id=None,
        now=clock.now(),
    )
    assert (state.trading_state, state.version) == (TradingState.PAUSED, 3)


def test_stale_expected_version_conflicts(db: Session, clock: ManualClock) -> None:
    _resume(db, clock)
    with pytest.raises(VersionConflict) as exc:
        _resume(db, clock, version=1)
    assert exc.value.current_version == 2
    assert get_system_state(db).version == 2


def test_same_state_is_noop_without_version_bump(db: Session, clock: ManualClock) -> None:
    state, event_id = set_trading_state(
        db,
        target=TradingState.PAUSED,
        expected_version=1,
        actor="op",
        reason="again",
        request_id=None,
        now=clock.now(),
    )
    assert (state.trading_state, state.version) == (TradingState.PAUSED, 1)
    row = list_audit(db, limit=1, before=None)[0]
    assert (row.id, row.result) == (event_id, "noop")
    assert _outbox_types(db) == []


def test_risk_state_change_emits_outbox_event_and_audits(db: Session, clock: ManualClock) -> None:
    state = set_risk_state(
        db, risk_state=RiskState.NORMAL, reason="reconciled", actor="worker", now=clock.now()
    )
    assert (state.risk_state, state.risk_reason, state.version) == (
        RiskState.NORMAL,
        "reconciled",
        2,
    )
    assert _outbox_types(db) == ["risk_state_changed"]
    row = list_audit(db, limit=1, before=None)[0]
    assert (row.action, row.actor) == ("risk_state.set", "worker")


def test_risk_state_same_state_is_noop(db: Session, clock: ManualClock) -> None:
    set_risk_state(db, risk_state=RiskState.NORMAL, reason="a", actor="w", now=clock.now())
    state = set_risk_state(db, risk_state=RiskState.NORMAL, reason="b", actor="w", now=clock.now())
    assert (state.version, state.risk_reason) == (2, "a")
    assert _outbox_types(db) == ["risk_state_changed"]


def test_risk_change_invalidates_operators_stale_version(db: Session, clock: ManualClock) -> None:
    set_risk_state(
        db, risk_state=RiskState.HALTED_DAILY_LOSS, reason="loss", actor="w", now=clock.now()
    )
    with pytest.raises(VersionConflict):
        _resume(db, clock, version=1)


def test_trading_enabled_requires_running_and_normal_risk(db: Session, clock: ManualClock) -> None:
    _resume(db, clock)
    assert get_system_state(db).trading_enabled is False  # risk still UNKNOWN
    set_risk_state(db, risk_state=RiskState.NORMAL, reason="ok", actor="w", now=clock.now())
    assert get_system_state(db).trading_enabled is True
    set_risk_state(
        db, risk_state=RiskState.HALTED_DRAWDOWN, reason="dd", actor="w", now=clock.now()
    )
    assert get_system_state(db).trading_enabled is False


def test_mark_reconciled_sets_timestamp_without_version_bump(
    db: Session, clock: ManualClock
) -> None:
    assert get_system_state(db).last_reconciled_at is None
    clock.advance(timedelta(minutes=1))
    mark_reconciled(db, now=clock.now())
    state = get_system_state(db)
    assert state.last_reconciled_at == clock.now()
    assert state.version == 1
