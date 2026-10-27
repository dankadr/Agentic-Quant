"""Advances operator commands (stop entries, cancel orders, flatten) on the executor.

Runs on a short cycle, independent of the one-minute strategy tick. Cancellation goes through
``ExecutionManager.cancel``, so every broker call keeps its existing fence and reconciliation
rules. A command completes only when the broker-side state proves it: no open or in-flight order
of the targeted kind, and, for a flatten, no held shares. Otherwise it stays in progress.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.control.commands import (
    CommandKind,
    CommandStatus,
    finish_command,
    open_commands,
)
from trading.control.state import TradingState, get_system_state, set_trading_state
from trading.core.clock import Clock
from trading.db.models.ops import OperationalCommandRow
from trading.db.models.trading import Order, OrderIntent
from trading.execution.manager import ExecutionManager
from trading.execution.state_machine import TERMINAL, OrderStatus
from trading.jobs.lease import Lease, assert_fence
from trading.quant.holdings import load_holdings

_EXECUTOR = "executor"
_NOT_TERMINAL = tuple(s.value for s in OrderStatus if s not in TERMINAL)


def _open_order_ids(session: Session, *, entries_only: bool) -> list[UUID]:
    """Orders not yet terminal, including in-flight ones: reconciliation owns the ambiguous ones."""
    stmt = (
        select(Order.id)
        .join(OrderIntent, OrderIntent.id == Order.intent_id)
        .where(Order.status.in_(_NOT_TERMINAL))
    )
    if entries_only:
        stmt = stmt.where(OrderIntent.is_entry.is_(True))
    return list(session.scalars(stmt))


class ControlSupervisor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        manager: ExecutionManager,
        *,
        clock: Clock,
        lease: Lease,
        account_id: UUID,
    ) -> None:
        self._sf = session_factory
        self._manager = manager
        self._clock = clock
        self._lease = lease
        self._account_id = account_id

    def run_cycle(self) -> None:
        with self._sf.begin() as session:
            assert_fence(session, self._lease, now=self._clock.now())
            command_ids = [row.id for row in open_commands(session)]
        for command_id in command_ids:
            self._advance(command_id)

    def _advance(self, command_id: UUID) -> None:
        with self._sf.begin() as session:
            row = session.get(OperationalCommandRow, command_id, with_for_update=True)
            if row is None or row.status not in (
                CommandStatus.REQUESTED.value,
                CommandStatus.IN_PROGRESS.value,
            ):
                return
            kind = CommandKind(row.kind)
        # Cancellations use their own transactions and fences; nothing here holds a row lock.
        for order_id in self._targets(kind):
            self._manager.cancel(order_id, reason=f"CONTROL_{kind.value}")
        with self._sf.begin() as session:
            now = self._clock.now()
            assert_fence(session, self._lease, now=now)
            row = session.get(OperationalCommandRow, command_id, with_for_update=True)
            if row is None or row.status not in (
                CommandStatus.REQUESTED.value,
                CommandStatus.IN_PROGRESS.value,
            ):
                return
            self._finish_or_wait(session, row, kind, now)

    def _targets(self, kind: CommandKind) -> list[UUID]:
        with self._sf() as session:
            return _open_order_ids(session, entries_only=kind is not CommandKind.CANCEL_OPEN_ORDERS)

    def _finish_or_wait(
        self, session: Session, row: OperationalCommandRow, kind: CommandKind, now: datetime
    ) -> None:
        remaining_entries = len(_open_order_ids(session, entries_only=True))
        remaining_all = len(_open_order_ids(session, entries_only=False))
        held = sum(1 for h in load_holdings(session, self._account_id, now) if h.qty > 0)
        progress = {
            "open_entry_orders": remaining_entries,
            "open_orders": remaining_all,
            "positions": held,
        }
        if kind is CommandKind.FLATTEN:
            done = remaining_all == 0 and held == 0
        elif kind is CommandKind.STOP_ENTRIES:
            done = remaining_entries == 0
        else:
            done = remaining_all == 0
        if not done:
            finish_command(
                session, row, status=CommandStatus.IN_PROGRESS, progress=progress, now=now
            )
            return
        finish_command(session, row, status=CommandStatus.COMPLETED, progress=progress, now=now)
        if kind is CommandKind.FLATTEN:
            # Flat with no open orders: stay in EXIT_ONLY so nothing re-enters.
            state = get_system_state(session, for_update=True)
            if state.trading_state is TradingState.FLATTENING:
                set_trading_state(
                    session,
                    target=TradingState.EXIT_ONLY,
                    expected_version=state.version,
                    actor=_EXECUTOR,
                    reason="flatten complete",
                    request_id=None,
                    now=now,
                )
