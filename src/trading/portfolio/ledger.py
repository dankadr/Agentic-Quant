"""Immutable fills and cash ledger. Corrections are reversal entries, never edits."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.brokers.base import BrokerFill
from trading.core.enums import PositionEffect, Side, effective_effect
from trading.core.errors import TradingError
from trading.core.money import quantize_cash, quantize_price, trade_cash_amount
from trading.db.models.portfolio import Account, CashLedgerEntry, Position
from trading.db.models.trading import FillRow, Order, OrderEvent
from trading.execution.state_machine import OrderStatus, assert_transition
from trading.jobs.outbox import emit_event
from trading.monitoring.audit import record_audit
from trading.portfolio.attribution import record_attribution


class LedgerInvariantError(TradingError):
    """A fill or entry would break a ledger invariant (overfill, oversell, double reversal...)."""


@dataclass(frozen=True)
class AppliedFill:
    fill_id: UUID
    order_id: UUID
    order_status: OrderStatus
    remaining_qty: Decimal
    position_qty: Decimal
    cash_delta: Decimal


def open_paper_account(
    session: Session, *, name: str, starting_cash: Decimal, now: datetime
) -> UUID:
    account = Account(
        name=name,
        broker="paper",
        mode="PAPER",
        currency="USD",
        status="ACTIVE",
        peak_equity=quantize_cash(starting_cash),
        created_at=now,
    )
    session.add(account)
    session.flush()
    session.add(
        CashLedgerEntry(
            account_id=account.id,
            ts=now,
            amount=quantize_cash(starting_cash),
            currency="USD",
            source_type="INITIAL_DEPOSIT",
            source_id=None,
            settles_on=now.date(),
            reverses_id=None,
        )
    )
    session.flush()
    return account.id


def apply_fill(
    session: Session,
    *,
    account_id: UUID,
    fill: BrokerFill,
    settles_on: date,
    now: datetime,
) -> AppliedFill | None:
    """Apply one broker fill transactionally: fill row, cash, position, order. Idempotent.

    Long sales settle on ``settles_on``. Short proceeds enter restricted cash immediately.
    All checks run before the first write, so a rejected fill leaves nothing behind.
    """
    session.get(Account, account_id, with_for_update=True)
    order = session.scalars(
        select(Order)
        .where(Order.client_order_id == fill.client_order_id, Order.account_id == account_id)
        .with_for_update()
    ).one_or_none()
    if order is None:
        raise LedgerInvariantError(
            f"fill {fill.execution_id} for unknown order {fill.client_order_id}"
        )
    if session.scalar(select(FillRow.id).where(FillRow.execution_id == fill.execution_id)):
        return None  # already applied
    if fill.side.value != order.side:
        raise LedgerInvariantError(f"fill {fill.execution_id} side does not match order")
    remaining = order.qty - order.filled_qty
    if fill.qty > remaining:
        raise LedgerInvariantError(
            f"overfill: {fill.execution_id} qty {fill.qty} exceeds remaining {remaining}"
        )
    new_filled = order.filled_qty + fill.qty
    previous_status = OrderStatus(order.status)
    new_status = OrderStatus.FILLED if new_filled == order.qty else OrderStatus.PARTIALLY_FILLED
    assert_transition(previous_status, new_status)

    position = session.get(Position, (account_id, order.instrument_id), with_for_update=True)
    effect = effective_effect(
        fill.side, PositionEffect(order.position_effect) if order.position_effect else None
    )
    held = position.qty if position else Decimal(0)
    directed = -held if effect.is_short else held
    if directed < 0 or (not effect.is_entry and fill.qty > directed):
        raise LedgerInvariantError(f"oversell or invalid cover: {effect} {fill.qty}, holds {held}")
    delta = fill.qty if fill.side is Side.BUY else -fill.qty

    # ---- all checks passed: write ----
    row = FillRow(
        order_id=order.id,
        execution_id=fill.execution_id,
        ts=fill.ts,
        qty=fill.qty,
        price=fill.price,
        fees=fill.fees,
        slippage_estimate=fill.slippage_estimate,
    )
    session.add(row)
    session.flush()

    trade_date = fill.ts.date()
    amount = trade_cash_amount(fill.side, fill.qty, fill.price)
    session.add(
        CashLedgerEntry(
            account_id=account_id,
            ts=fill.ts,
            amount=amount,
            currency="USD",
            source_type="TRADE",
            source_id=str(row.id),
            settles_on=settles_on if effect is PositionEffect.CLOSE_LONG else trade_date,
            reverses_id=None,
        )
    )
    if fill.fees > 0:
        session.add(
            CashLedgerEntry(
                account_id=account_id,
                ts=fill.ts,
                amount=-fill.fees,
                currency="USD",
                source_type="FEE",
                source_id=str(row.id),
                settles_on=trade_date,
                reverses_id=None,
            )
        )

    realized = -fill.fees
    if position is None:
        position = Position(
            account_id=account_id,
            instrument_id=order.instrument_id,
            qty=Decimal(0),
            average_cost=Decimal(0),
            realized_pnl=Decimal(0),
            updated_at=now,
        )
        session.add(position)
    if effect.is_entry:
        position.average_cost = quantize_price(
            (abs(held) * position.average_cost + fill.qty * fill.price) / (abs(held) + fill.qty)
        )
    else:
        realized += (position.average_cost - fill.price) * delta
    position.qty = held + delta
    position.realized_pnl = quantize_cash(position.realized_pnl + realized)
    position.updated_at = now

    record_attribution(
        session, order=order, fill_id=row.id, fill=fill, realized=quantize_cash(realized)
    )

    order.avg_fill_price = quantize_price(
        ((order.avg_fill_price or Decimal(0)) * order.filled_qty + fill.price * fill.qty)
        / new_filled
    )
    order.filled_qty = new_filled
    order.status = new_status.value
    order.version += 1
    order.updated_at = now
    session.add(
        OrderEvent(
            order_id=order.id,
            ts=now,
            from_status=previous_status.value,
            to_status=new_status.value,
            reason="fill",
            payload_json={
                "execution_id": fill.execution_id,
                "qty": str(fill.qty),
                "price": str(fill.price),
            },
        )
    )
    session.flush()
    emit_event(
        session,
        event_type="fill_applied",
        payload={
            "execution_id": fill.execution_id,
            "client_order_id": fill.client_order_id,
            "qty": fill.qty,
            "price": fill.price,
            "order_status": new_status.value,
        },
        now=now,
    )
    return AppliedFill(
        fill_id=row.id,
        order_id=order.id,
        order_status=new_status,
        remaining_qty=order.qty - new_filled,
        position_qty=position.qty,
        cash_delta=amount - fill.fees,
    )


def reverse_ledger_entry(session: Session, *, entry_id: UUID, reason: str, now: datetime) -> UUID:
    """Append an offsetting entry. The original is never modified."""
    entry = session.get(CashLedgerEntry, entry_id, with_for_update=True)
    if entry is None:
        raise LedgerInvariantError(f"ledger entry {entry_id} not found")
    if entry.source_type == "REVERSAL":
        raise LedgerInvariantError("cannot reverse a reversal entry")
    already = session.scalar(
        select(CashLedgerEntry.id).where(CashLedgerEntry.reverses_id == entry_id)
    )
    if already is not None:
        raise LedgerInvariantError(f"entry {entry_id} already reversed")
    reversal = CashLedgerEntry(
        account_id=entry.account_id,
        ts=now,
        amount=-entry.amount,
        currency=entry.currency,
        source_type="REVERSAL",
        source_id=str(entry_id),
        settles_on=now.date(),
        reverses_id=entry_id,
    )
    session.add(reversal)
    session.flush()
    record_audit(
        session,
        actor="system",
        action="ledger.reverse",
        target=str(entry_id),
        request_id=None,
        before=None,
        after=None,
        result="ok",
        now=now,
        details={"reason": reason, "reversal_id": str(reversal.id)},
    )
    return reversal.id
