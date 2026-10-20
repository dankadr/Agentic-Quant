"""Versioned system state: trading on/off and risk state. Survives restarts (fail closed)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.errors import TradingError
from trading.db.models.ops import SystemStateRow
from trading.jobs.outbox import emit_event
from trading.monitoring.audit import record_audit


class TradingState(StrEnum):
    PAUSED = "PAUSED"
    PAPER_RUNNING = "PAPER_RUNNING"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    HALTED_DAILY_LOSS = "HALTED_DAILY_LOSS"
    HALTED_DRAWDOWN = "HALTED_DRAWDOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    UNKNOWN = "UNKNOWN"


class VersionConflict(TradingError):
    def __init__(self, current_version: int) -> None:
        super().__init__(f"stale version; current version is {current_version}")
        self.current_version = current_version


@dataclass(frozen=True)
class SystemState:
    mode: Literal["PAPER_ONLY"]
    trading_state: TradingState
    risk_state: RiskState
    risk_reason: str | None
    version: int
    updated_at: datetime
    last_reconciled_at: datetime | None

    @property
    def trading_enabled(self) -> bool:
        return (
            self.trading_state is TradingState.PAPER_RUNNING and self.risk_state is RiskState.NORMAL
        )


def _to_state(row: SystemStateRow) -> SystemState:
    return SystemState(
        mode="PAPER_ONLY",
        trading_state=TradingState(row.trading_state),
        risk_state=RiskState(row.risk_state),
        risk_reason=row.risk_reason,
        version=row.version,
        updated_at=row.updated_at,
        last_reconciled_at=row.last_reconciled_at,
    )


def _row(session: Session, *, for_update: bool) -> SystemStateRow:
    stmt = select(SystemStateRow).where(SystemStateRow.id == 1)
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one()


def get_system_state(session: Session, *, for_update: bool = False) -> SystemState:
    return _to_state(_row(session, for_update=for_update))


def set_trading_state(
    session: Session,
    *,
    target: TradingState,
    expected_version: int,
    actor: str,
    reason: str,
    request_id: str | None,
    now: datetime,
) -> tuple[SystemState, UUID]:
    """Compare-and-set the trading state. Same-state requests are audited no-ops."""
    row = _row(session, for_update=True)
    if row.version != expected_version:
        raise VersionConflict(row.version)
    before = {"trading_state": row.trading_state, "version": row.version}
    if row.trading_state == target.value:
        event_id = record_audit(
            session,
            actor=actor,
            action="trading_state.set",
            target="system_state",
            request_id=request_id,
            before=before,
            after=before,
            result="noop",
            now=now,
            details={"reason": reason},
        )
        return _to_state(row), event_id
    previous = row.trading_state
    row.trading_state = target.value
    row.version += 1
    row.updated_at = now
    session.flush()
    event_id = record_audit(
        session,
        actor=actor,
        action="trading_state.set",
        target="system_state",
        request_id=request_id,
        before=before,
        after={"trading_state": row.trading_state, "version": row.version},
        result="ok",
        now=now,
        details={"reason": reason},
    )
    emit_event(
        session,
        event_type="trading_state_changed",
        payload={
            "from": previous,
            "to": target.value,
            "reason": reason,
            "actor": actor,
            "version": row.version,
        },
        now=now,
    )
    return _to_state(row), event_id


def set_risk_state(
    session: Session, *, risk_state: RiskState, reason: str, actor: str, now: datetime
) -> SystemState:
    """Change the risk state. Bumps the version so operators acting on stale reads get a 409."""
    row = _row(session, for_update=True)
    if row.risk_state == risk_state.value:
        return _to_state(row)
    previous = row.risk_state
    before = {"risk_state": previous, "version": row.version}
    row.risk_state = risk_state.value
    row.risk_reason = reason
    row.version += 1
    row.updated_at = now
    session.flush()
    record_audit(
        session,
        actor=actor,
        action="risk_state.set",
        target="system_state",
        request_id=None,
        before=before,
        after={"risk_state": row.risk_state, "version": row.version},
        result="ok",
        now=now,
        details={"reason": reason},
    )
    emit_event(
        session,
        event_type="risk_state_changed",
        payload={
            "from": previous,
            "to": risk_state.value,
            "reason": reason,
            "version": row.version,
        },
        now=now,
    )
    return _to_state(row)


def mark_reconciled(session: Session, *, now: datetime) -> None:
    _row(session, for_update=True).last_reconciled_at = now
    session.flush()
