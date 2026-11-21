"""Apply splits and cash dividends to held positions, exactly once per (account, action)."""

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from sqlalchemy import String, cast, exists, literal, select
from sqlalchemy.orm import Session

from trading.brokers.paper.broker import PaperBroker
from trading.core.money import quantize_cash, quantize_price
from trading.data.calendar import NEW_YORK
from trading.db.models.market import CorporateAction, Instrument
from trading.db.models.ops import AuditEventRow
from trading.db.models.portfolio import CashLedgerEntry, Position
from trading.db.models.trading import FillRow, Order
from trading.monitoring.audit import record_audit
from trading.portfolio.ledger import LedgerInvariantError
from trading.quant.holdings import load_holdings

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
    dividends: dict[str, Decimal] = {}
    previous = position.qty if position is not None else Decimal(0)
    result = "noop"
    if position is not None and position.qty != 0:
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
            symbol = session.scalar(
                select(Instrument.symbol).where(Instrument.id == action.instrument_id)
            )
            owners = [h for h in load_holdings(session, account_id, now) if h.symbol == symbol]
            dividends = {h.strategy_id: quantize_cash(h.qty * action.cash_amount) for h in owners}
            if owners and sum(h.qty for h in owners) == position.qty:
                anchor = max(owners, key=lambda h: (abs(h.qty), h.strategy_id)).strategy_id
                dividends[anchor] += amount - sum(dividends.values())
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
        details={
            "action_type": action.action_type,
            "dividend_pnl": {k: str(v) for k, v in dividends.items()},
            "qty_delta": str(position.qty - previous if position else 0),
        },
    )
    return True


def apply_due_actions(
    session: Session,
    *,
    account_id: UUID,
    broker: PaperBroker,
    session_date: date,
    now: datetime,
    provider: str,
) -> None:
    """Apply visible actions to both independent books in the same fenced transaction."""
    processed = exists().where(
        AuditEventRow.action == _MARKER,
        AuditEventRow.target == literal(f"{account_id}:") + cast(CorporateAction.id, String),
    )
    due = session.execute(
        select(CorporateAction, Instrument.symbol)
        .join(Instrument, CorporateAction.instrument_id == Instrument.id)
        .where(
            CorporateAction.provider == provider,
            CorporateAction.ex_date <= session_date,
            CorporateAction.announced_at <= now,
            CorporateAction.ingested_at <= now,
            ~processed,
        )
        .order_by(
            CorporateAction.ex_date, (CorporateAction.action_type != "SPLIT"), CorporateAction.id
        )
    ).all()
    for action, symbol in due:
        if action.action_type == "SPLIT":
            if (
                not action.ratio_from
                or not action.ratio_to
                or min(action.ratio_from, action.ratio_to) <= 0
            ):
                raise LedgerInvariantError(f"invalid split {action.id}")
        elif (
            action.action_type != "CASH_DIVIDEND"
            or action.cash_amount is None
            or action.cash_amount < 0
        ):
            raise LedgerInvariantError(f"invalid corporate action {action.id}")
        cutoff = datetime.combine(action.ex_date, time.min, NEW_YORK)
        trades = (
            select(FillRow.id)
            .join(Order, Order.id == FillRow.order_id)
            .where(Order.account_id == account_id, Order.instrument_id == action.instrument_id)
        )
        if session.scalar(trades.where(FillRow.ts >= cutoff).limit(1)):
            if session.scalar(trades.where(FillRow.ts < cutoff).limit(1)):
                raise LedgerInvariantError(f"late action requires reconciliation: {action.id}")
            # The account first traded this instrument after the ex-date.
            record_audit(
                session,
                actor="system",
                action=_MARKER,
                target=f"{account_id}:{action.id}",
                request_id=None,
                before=None,
                after=None,
                result="noop",
                now=now,
                details={"action_type": action.action_type},
            )
            continue
        position = session.get(Position, (account_id, action.instrument_id), with_for_update=True)
        broker.apply_action(
            session,
            action=action,
            symbol=symbol,
            expected_qty=position.qty if position else Decimal(0),
        )
        apply_corporate_action(session, account_id=account_id, action=action, now=now)
