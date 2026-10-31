"""Operator commands that change what the system may do, tracked until the executor finishes them.

The request is recorded in the caller's transaction together with the trading-state change, so an
accepted command can never exist without its state change, or the reverse. The executor completes
the command in ``trading.execution.controls``; the operator polls it by id.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.control.state import (
    RiskState,
    SystemState,
    TradingState,
    get_system_state,
    set_trading_state,
)
from trading.core.errors import TradingError
from trading.db.models.ops import OperationalCommandRow
from trading.jobs.outbox import emit_event


class CommandKind(StrEnum):
    STOP_ENTRIES = "STOP_ENTRIES"
    CANCEL_OPEN_ORDERS = "CANCEL_OPEN_ORDERS"
    FLATTEN = "FLATTEN"


class CommandStatus(StrEnum):
    REQUESTED = "REQUESTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


OPEN_STATUSES = (CommandStatus.REQUESTED.value, CommandStatus.IN_PROGRESS.value)
_TARGET_STATE = {
    CommandKind.STOP_ENTRIES: TradingState.EXIT_ONLY,
    CommandKind.CANCEL_OPEN_ORDERS: TradingState.EXIT_ONLY,
    CommandKind.FLATTEN: TradingState.FLATTENING,
}


class CommandNotFound(TradingError):
    """No operational command has this id."""


class CommandNotAllowed(TradingError):
    """The command is refused in the current state (nothing was recorded or changed)."""


_UNSAFE_TO_LIQUIDATE = frozenset({RiskState.UNKNOWN, RiskState.RECONCILIATION_REQUIRED})


@dataclass(frozen=True)
class CommandView:
    id: UUID
    kind: CommandKind
    status: CommandStatus
    reason: str
    requested_at: datetime
    completed_at: datetime | None
    progress: dict[str, Any]


def _view(row: OperationalCommandRow) -> CommandView:
    return CommandView(
        id=row.id,
        kind=CommandKind(row.kind),
        status=CommandStatus(row.status),
        reason=row.reason,
        requested_at=row.requested_at,
        completed_at=row.completed_at,
        progress=dict(row.progress_json),
    )


def request_command(
    session: Session,
    *,
    kind: CommandKind,
    actor: str,
    reason: str,
    expected_version: int,
    request_id: str | None,
    now: datetime,
) -> tuple[CommandView, SystemState]:
    """Record the command and move the trading state, compare-and-set on ``expected_version``.

    The most restrictive state always wins. A stop or cancel never lifts a pause or downgrades an
    in-progress flatten; cancellation still runs while paused, because cancelling is not submitting.
    Flatten out of PAUSED is the one explicit way to liquidate from a pause, and it is refused while
    the risk state is unknown or needs reconciliation.
    """
    current = get_system_state(session, for_update=True)
    target = _TARGET_STATE[kind]
    if current.trading_state is TradingState.PAUSED and kind is not CommandKind.FLATTEN:
        target = TradingState.PAUSED
    elif current.trading_state is TradingState.FLATTENING and kind is not CommandKind.FLATTEN:
        target = TradingState.FLATTENING
    if kind is CommandKind.FLATTEN and current.risk_state in _UNSAFE_TO_LIQUIDATE:
        raise CommandNotAllowed(
            f"cannot flatten while the risk state is {current.risk_state.value}"
        )
    state, _ = set_trading_state(
        session,
        target=target,
        expected_version=expected_version,
        actor=actor,
        reason=reason,
        request_id=request_id,
        now=now,
    )
    row = OperationalCommandRow(
        id=uuid4(),
        kind=kind.value,
        status=CommandStatus.REQUESTED.value,
        requested_by=actor,
        request_id=request_id,
        reason=reason,
        requested_version=expected_version,
        requested_at=now,
        updated_at=now,
        progress_json={},
    )
    session.add(row)
    session.flush()
    emit_event(
        session,
        event_type="operational_command_requested",
        payload={"command_id": str(row.id), "kind": kind.value, "actor": actor},
        now=now,
    )
    return _view(row), state


def get_command(session: Session, command_id: UUID) -> CommandView:
    row = session.get(OperationalCommandRow, command_id)
    if row is None:
        raise CommandNotFound(f"no operational command {command_id}")
    return _view(row)


def open_commands(session: Session) -> list[OperationalCommandRow]:
    """Unfinished commands, oldest first. The executor advances these each supervision cycle."""
    return list(
        session.scalars(
            select(OperationalCommandRow)
            .where(OperationalCommandRow.status.in_(OPEN_STATUSES))
            .order_by(OperationalCommandRow.requested_at, OperationalCommandRow.id)
            .with_for_update()
        )
    )


def finish_command(
    session: Session,
    row: OperationalCommandRow,
    *,
    status: CommandStatus,
    progress: dict[str, Any],
    now: datetime,
) -> None:
    row.status = status.value
    row.progress_json = progress
    row.updated_at = now
    if status in (CommandStatus.COMPLETED, CommandStatus.FAILED):
        row.completed_at = now
        emit_event(
            session,
            event_type="operational_command_finished",
            payload={"command_id": str(row.id), "kind": row.kind, "status": status.value},
            now=now,
        )
    session.flush()
