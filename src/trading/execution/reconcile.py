"""Fail-closed reconciliation of the application ledger against the broker.

It resolves orders left in SUBMITTING/UNKNOWN by asking the broker what exists; it NEVER
resubmits. An order the broker has no record of is rejected only after a grace period, and any
new attempt needs a fresh risk decision. Any mismatch, pending item or error puts the system in
RECONCILIATION_REQUIRED, which blocks all new orders (entries and exits) until a clean run.
"""

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.base import (
    BrokerAdapter,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.control.state import RiskState, get_system_state, mark_reconciled, set_risk_state
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.errors import TradingError
from trading.data.calendar import MarketCalendar
from trading.db.models.market import Instrument
from trading.db.models.portfolio import CashLedgerEntry, Position, ReconciliationRun
from trading.db.models.trading import FillRow, Order
from trading.execution.manager import ExecutionManager
from trading.execution.state_machine import OPEN_AT_BROKER, TERMINAL, OrderStatus
from trading.execution.transitions import transition_order
from trading.jobs.lease import Lease, LeaseLost, assert_fence
from trading.jobs.outbox import emit_event
from trading.monitoring.audit import record_audit
from trading.risk.halts import restore_halt_state

_CASH_TOLERANCE = Decimal("0.0001")
_LIMBO = (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN)
# Nothing reaches the broker before SUBMITTING commits, so these can only be abandoned work.
_ABANDONED = (OrderStatus.CREATED, OrderStatus.RISK_APPROVED)
_CANDIDATES = (*_ABANDONED, *_LIMBO, *OPEN_AT_BROKER)
_REJECTED_LOCALLY = (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED)


@dataclass(frozen=True)
class Mismatch:
    # ORPHAN_BROKER_ORDER | LATE_DELIVERY | STATUS | MISSING_AT_BROKER | FILLED_QTY | POSITION
    # | CASH | LEDGER_INTERNAL
    kind: str
    subject: str
    local: str
    broker: str


@dataclass(frozen=True)
class ReconciliationReport:
    run_id: UUID
    status: str  # OK | PENDING | MISMATCH | ERROR
    resolved: tuple[str, ...]
    pending: tuple[str, ...]
    mismatches: tuple[Mismatch, ...]


class _Collector:
    def __init__(self) -> None:
        self.resolved: list[str] = []
        self.pending: list[str] = []
        self.mismatches: list[Mismatch] = []


def reconcile_account(
    session_factory: sessionmaker[Session],
    *,
    account_id: UUID,
    broker: BrokerAdapter,
    clock: Clock,
    lease: Lease,
    calendar: MarketCalendar,
    config: AppConfig,
) -> ReconciliationReport:
    started = clock.now()
    found = _Collector()
    error: str | None = None
    try:
        ExecutionManager(
            session_factory, broker, clock=clock, lease=lease, calendar=calendar, config=config
        ).sync_fills()
        grace = timedelta(seconds=config.risk.unknown_order_grace_seconds)
        _resolve_orders(session_factory, broker, clock, lease, account_id, grace, found)
        _compare_books(session_factory, broker, account_id, found)
    except LeaseLost:
        raise  # we are no longer the executor: write nothing, let the worker stop
    except TradingError as exc:
        error = f"{type(exc).__name__}: {exc}"

    resolved = tuple(sorted(found.resolved))
    pending = tuple(sorted(found.pending))
    mismatches = tuple(sorted(found.mismatches, key=lambda m: (m.kind, m.subject)))
    status = "ERROR" if error else "MISMATCH" if mismatches else "PENDING" if pending else "OK"
    report_json: dict[str, Any] = {
        "resolved": list(resolved),
        "pending": list(pending),
        "mismatches": [m.__dict__ for m in mismatches],
        "error": error,
    }
    with session_factory.begin() as session:
        now = clock.now()
        assert_fence(session, lease, now=now)
        run = ReconciliationRun(
            account_id=account_id,
            started_at=started,
            completed_at=now,
            status=status,
            report_json=report_json,
        )
        session.add(run)
        session.flush()
        _apply_risk_outcome(session, account_id, status, report_json, now)
        record_audit(
            session,
            actor="system",
            action="reconciliation.run",
            target=str(run.id),
            request_id=None,
            before=None,
            after=None,
            result=status,
            now=now,
            details={
                "resolved": len(resolved),
                "pending": len(pending),
                "mismatches": len(mismatches),
            },
        )
        run_id = run.id
    return ReconciliationReport(run_id, status, resolved, pending, mismatches)


def _apply_risk_outcome(
    session: Session, account_id: UUID, status: str, report_json: dict[str, Any], now: datetime
) -> None:
    current = get_system_state(session, for_update=True).risk_state
    if status == "OK":
        mark_reconciled(session, now=now)
        if current in (RiskState.UNKNOWN, RiskState.RECONCILIATION_REQUIRED):
            # A halt that was in force before the reconciliation problem must come back.
            halt = restore_halt_state(session, account_id)
            set_risk_state(
                session,
                risk_state=halt or RiskState.NORMAL,
                reason="reconciled" if halt is None else "reconciled; halt restored",
                actor="system",
                now=now,
            )
        return
    if current is not RiskState.RECONCILIATION_REQUIRED:
        set_risk_state(
            session,
            risk_state=RiskState.RECONCILIATION_REQUIRED,
            reason=f"reconciliation {status}",
            actor="system",
            now=now,
        )
    if status in ("MISMATCH", "ERROR"):
        emit_event(
            session,
            event_type="risk_alert",
            payload={
                "kind": f"RECONCILIATION_{status}",
                "mismatches": len(report_json["mismatches"]),
                "error": report_json["error"],
            },
            now=now,
        )


# ---- step 2/3: orders ------------------------------------------------------------------------


def _resolve_orders(
    sf: sessionmaker[Session],
    broker: BrokerAdapter,
    clock: Clock,
    lease: Lease,
    account_id: UUID,
    grace: timedelta,
    found: _Collector,
) -> None:
    with sf() as session:
        candidates = session.execute(
            select(Order.id, Order.client_order_id)
            .where(
                Order.account_id == account_id,
                Order.status.in_([s.value for s in _CANDIDATES]),
            )
            .order_by(Order.client_order_id)
        ).all()
    for order_id, client_order_id in candidates:
        with sf.begin() as session:
            now = clock.now()
            assert_fence(session, lease, now=now)
            order = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).one()
            if OrderStatus(order.status) in _ABANDONED:
                transition_order(
                    session,
                    order,
                    OrderStatus.REJECTED,
                    reason="ABANDONED_BEFORE_SUBMIT",
                    now=now,
                )
                found.resolved.append(client_order_id)
                continue
            state = broker.get_order(client_order_id)
            _reconcile_order(session, order, state, now, grace, found)
            if state is not None and state.filled_qty != order.filled_qty:
                found.mismatches.append(
                    Mismatch(
                        "FILLED_QTY", client_order_id, str(order.filled_qty), str(state.filled_qty)
                    )
                )
    with sf() as session:
        local_status = dict(session.execute(select(Order.client_order_id, Order.status)).all())
    for state in broker.list_open_orders():
        coid = state.client_order_id
        local = local_status.get(coid)
        if local is None:
            found.mismatches.append(
                Mismatch("ORPHAN_BROKER_ORDER", coid, "absent", state.status.value)
            )
        elif OrderStatus(local) in _REJECTED_LOCALLY:
            # A stale worker's submit landed after we gave up on the order: stop it resting.
            found.mismatches.append(Mismatch("LATE_DELIVERY", coid, local, state.status.value))
            with suppress(BrokerRejected, BrokerTimeout, BrokerUnavailable):
                broker.cancel_order(coid)
        elif OrderStatus(local) in TERMINAL:
            found.mismatches.append(Mismatch("STATUS", coid, local, state.status.value))


def _reconcile_order(
    session: Session,
    order: Order,
    state: BrokerOrderState | None,
    now: datetime,
    grace: timedelta,
    found: _Collector,
) -> None:
    status = OrderStatus(order.status)
    limbo = status in _LIMBO
    coid = order.client_order_id
    if state is None:
        if not limbo:
            found.mismatches.append(Mismatch("MISSING_AT_BROKER", coid, status.value, "absent"))
        elif now - order.updated_at >= grace:
            transition_order(
                session, order, OrderStatus.REJECTED, reason="NOT_FOUND_AT_BROKER", now=now
            )
            found.resolved.append(coid)
        else:
            found.pending.append(coid)
        return
    broker_status = state.status
    if broker_status is BrokerOrderStatus.REJECTED:
        if limbo:
            transition_order(
                session, order, OrderStatus.REJECTED, reason="BROKER_REJECTED", now=now
            )
            found.resolved.append(coid)
        return
    if broker_status in (
        BrokerOrderStatus.ACCEPTED,
        BrokerOrderStatus.PARTIALLY_FILLED,
        BrokerOrderStatus.FILLED,
    ):
        if limbo:
            transition_order(
                session,
                order,
                OrderStatus.ACKNOWLEDGED,
                reason=None,
                now=now,
                broker_order_id=state.broker_order_id,
            )
            found.resolved.append(coid)
        return
    target = (
        OrderStatus.CANCELED if broker_status is BrokerOrderStatus.CANCELED else OrderStatus.EXPIRED
    )
    if status is OrderStatus.SUBMITTING:  # the table has no SUBMITTING -> CANCELED/EXPIRED edge
        transition_order(
            session,
            order,
            OrderStatus.ACKNOWLEDGED,
            reason=None,
            now=now,
            broker_order_id=state.broker_order_id,
        )
    transition_order(session, order, target, reason=f"BROKER_{target.value}", now=now)
    if limbo:
        found.resolved.append(coid)


# ---- step 4/5/6: books -------------------------------------------------------------------------


def _compare_books(
    sf: sessionmaker[Session], broker: BrokerAdapter, account_id: UUID, found: _Collector
) -> None:
    account = broker.get_account()
    with sf() as session:
        local_positions = {
            symbol: qty
            for symbol, qty in session.execute(
                select(Instrument.symbol, Position.qty)
                .join(Instrument, Instrument.id == Position.instrument_id)
                .where(Position.account_id == account_id, Position.qty > 0)
            )
        }
        local_cash = session.scalar(
            select(func.coalesce(func.sum(CashLedgerEntry.amount), 0)).where(
                CashLedgerEntry.account_id == account_id
            )
        ) or Decimal(0)
        order_fill_gaps = session.execute(
            select(Order.client_order_id, Order.filled_qty, func.coalesce(func.sum(FillRow.qty), 0))
            .outerjoin(FillRow, FillRow.order_id == Order.id)
            .where(Order.account_id == account_id)
            .group_by(Order.id)
        ).all()
        held_by_symbol = {
            symbol: qty
            for symbol, qty in session.execute(
                select(Instrument.symbol, Position.qty)
                .join(Instrument, Instrument.id == Position.instrument_id)
                .where(Position.account_id == account_id)
            )
        }
        signed_qty = case((Order.side == "BUY", FillRow.qty), else_=-FillRow.qty)
        net_from_fills = {
            symbol: net
            for symbol, net in session.execute(
                select(Instrument.symbol, func.sum(signed_qty))
                .select_from(FillRow)
                .join(Order, Order.id == FillRow.order_id)
                .join(Instrument, Instrument.id == Order.instrument_id)
                .where(Order.account_id == account_id)
                .group_by(Instrument.symbol)
            )
        }
    for symbol in sorted(set(local_positions) | set(account.positions)):
        local, remote = (
            local_positions.get(symbol, Decimal(0)),
            account.positions.get(symbol, Decimal(0)),
        )
        if local != remote:
            found.mismatches.append(Mismatch("POSITION", symbol, str(local), str(remote)))
    if abs(local_cash - account.cash) > _CASH_TOLERANCE:
        found.mismatches.append(Mismatch("CASH", "account", str(local_cash), str(account.cash)))
    for coid, filled, fills_total in order_fill_gaps:
        if filled != fills_total:
            found.mismatches.append(
                Mismatch("LEDGER_INTERNAL", f"order:{coid}", str(filled), str(fills_total))
            )
    for symbol in sorted(set(held_by_symbol) | set(net_from_fills)):
        held = held_by_symbol.get(symbol, Decimal(0))
        from_fills = net_from_fills.get(symbol, Decimal(0))
        if held != from_fills:
            found.mismatches.append(
                Mismatch("LEDGER_INTERNAL", f"position:{symbol}", str(held), str(from_fills))
            )
