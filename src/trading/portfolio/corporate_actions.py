"""Apply splits and cash dividends to held positions, exactly once per (account, action)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.money import quantize_cash, quantize_price
from trading.db.models.market import CorporateAction
from trading.db.models.ops import AuditEventRow
from trading.db.models.portfolio import CashLedgerEntry, Position
from trading.monitoring.audit import record_audit
from trading.portfolio.ledger import LedgerInvariantError

_MARKER = "corporate_action.applied"


def apply_corporate_action(
    session: Session, *, account_id: UUID, action: CorporateAction, now: datetime
) -> bool:
    """Process ``action`` for the account. Returns False if it was already processed."""
    target = f"{account_id}:{action.id}"
    if session.scalar(
        select(AuditEventRow.id).where(
            AuditEventRow.action == _MARKER, AuditEventRow.target == target
        )
    ):
        return False
    position = session.get(Position, (account_id, action.instrument_id), with_for_update=True)
    result = "noop"
    if position is not None and position.qty > 0:
        if action.action_type == "SPLIT":
            if action.ratio_from is None or action.ratio_to is None:
                raise LedgerInvariantError(f"split {action.id} has no ratio")
            factor = action.ratio_to / action.ratio_from
            position.qty = position.qty * factor
            position.average_cost = quantize_price(position.average_cost / factor)
        else:
            if action.cash_amount is None:
                raise LedgerInvariantError(f"dividend {action.id} has no cash amount")
            amount = quantize_cash(position.qty * action.cash_amount)
            session.add(
                CashLedgerEntry(
                    account_id=account_id,
                    ts=now,
                    amount=amount,
                    currency="USD",
                    source_type="DIVIDEND",
                    source_id=str(action.id),
                    settles_on=now.date(),
                    reverses_id=None,
                )
            )
            position.realized_pnl = quantize_cash(position.realized_pnl + amount)
        position.updated_at = now
        result = "ok"
    session.flush()
    record_audit(
        session,
        actor="system",
        action=_MARKER,
        target=target,
        request_id=None,
        before=None,
        after=None,
        result=result,
        now=now,
        details={"action_type": action.action_type},
    )
    return True
