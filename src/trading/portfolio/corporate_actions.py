"""Apply splits and cash dividends to held positions, exactly once per (account, action)."""

import csv
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from pathlib import Path
from uuid import UUID

from sqlalchemy import String, cast, exists, func, literal, select
from sqlalchemy.orm import Session

from trading.brokers.paper.broker import PaperBroker
from trading.core.money import quantize_cash, split_position
from trading.data.alpaca_provider import PROVIDER_NAME
from trading.data.calendar import NEW_YORK, MarketCalendar
from trading.data.ingest import store_corporate_actions
from trading.data.models import CorporateActionRecord, validate_action_terms
from trading.db.models.market import CorporateAction, Instrument
from trading.db.models.ops import AuditEventRow
from trading.db.models.portfolio import CashLedgerEntry, Position
from trading.db.models.trading import FillRow, Order
from trading.monitoring.audit import record_audit
from trading.portfolio.ledger import LedgerInvariantError
from trading.quant.holdings import load_holdings

_MARKER = "corporate_action.applied"
_COVERAGE = "corporate_action.coverage_verified"


def verify_paper_actions(
    session: Session,
    path: Path,
    day: date,
    symbols: list[str],
    now: datetime,
) -> None:
    """Attest complete ex-date coverage for the configured online universe in one transaction."""
    if day > now.astimezone(NEW_YORK).date():
        raise ValueError("cannot verify future action coverage")
    raw = (path / "corporate_actions.csv").read_bytes()
    rows = csv.DictReader(StringIO(raw.decode("utf-8-sig")))
    required = {"symbol", "action_type", "ex_date", "announced_at"}
    if not required <= set(rows.fieldnames or []):
        raise ValueError("corporate_actions.csv requires " + ", ".join(sorted(required)))
    actions = []
    expected = {}
    for row in rows:
        action = CorporateActionRecord.model_validate(
            {
                **{k: v for k, v in row.items() if v},
                "provider": PROVIDER_NAME,
            }
        )
        key = action.symbol, action.action_type
        if (
            action.symbol not in symbols
            or action.ex_date != day
            or action.announced_at > now
            or key in expected
        ):
            raise ValueError(f"duplicate, out-of-scope or future corporate action: {key}")
        expected[key] = (
            action.ratio_from,
            action.ratio_to,
            action.cash_amount,
            action.announced_at,
        )
        actions.append(action)
    store_corporate_actions(session, actions, now)
    stored = session.execute(
        select(Instrument.symbol, CorporateAction)
        .join(CorporateAction, CorporateAction.instrument_id == Instrument.id)
        .where(
            Instrument.symbol.in_(symbols),
            CorporateAction.ex_date == day,
            CorporateAction.provider == PROVIDER_NAME,
        )
    )
    actual = {
        (symbol, a.action_type): (a.ratio_from, a.ratio_to, a.cash_amount, a.announced_at)
        for symbol, a in stored
    }
    if actual != expected:
        raise ValueError("verified CSV conflicts with stored corporate actions")
    digest = sha256(raw).hexdigest()
    for symbol in symbols:
        record_audit(
            session,
            actor="operator",
            action=_COVERAGE,
            target=f"{PROVIDER_NAME}:{day}:{symbol}",
            request_id=None,
            before=None,
            after=None,
            result="ok",
            now=now,
            details={"csv_sha256": digest},
        )


def require_action_coverage(
    session: Session,
    account_id: UUID,
    day: date,
    now: datetime,
    calendar: MarketCalendar,
) -> None:
    """Require verified sessions since the last fill of every carried short."""
    shorts = session.execute(
        select(Instrument.symbol, Position.instrument_id, Position.updated_at)
        .join(Instrument, Instrument.id == Position.instrument_id)
        .where(Position.account_id == account_id, Position.qty < 0)
    )
    for symbol, instrument_id, updated in shorts:
        latest = session.scalar(
            select(func.max(FillRow.ts))
            .join(Order, Order.id == FillRow.order_id)
            .where(Order.account_id == account_id, Order.instrument_id == instrument_id)
        )
        cursor = (latest or updated).astimezone(NEW_YORK).date() + timedelta(days=1)
        while cursor <= day:
            if calendar.session_on(cursor) and not session.scalar(
                select(AuditEventRow.id)
                .where(
                    AuditEventRow.action == _COVERAGE,
                    AuditEventRow.result == "ok",
                    AuditEventRow.target == f"{PROVIDER_NAME}:{cursor}:{symbol}",
                    AuditEventRow.ts <= now,
                )
                .limit(1)
            ):
                raise LedgerInvariantError(
                    f"unverified corporate action coverage for {symbol} on {cursor}"
                )
            cursor += timedelta(days=1)


def _validate_stored_action(action: CorporateAction) -> None:
    try:
        validate_action_terms(
            action.action_type, action.ratio_from, action.ratio_to, action.cash_amount
        )
    except ValueError as exc:
        raise LedgerInvariantError(f"invalid corporate action {action.id}: {exc}") from exc


def apply_corporate_action(
    session: Session, *, account_id: UUID, action: CorporateAction, now: datetime
) -> bool:
    """Process ``action`` for the account. Returns False if it was already processed."""
    _validate_stored_action(action)
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
            position.qty, position.average_cost = split_position(
                position.qty, position.average_cost, action.ratio_from, action.ratio_to
            )
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
        _validate_stored_action(action)
        later_applied = session.scalar(
            select(CorporateAction.id)
            .join(
                AuditEventRow,
                AuditEventRow.target
                == literal(f"{account_id}:") + cast(CorporateAction.id, String),
            )
            .where(
                CorporateAction.instrument_id == action.instrument_id,
                AuditEventRow.action == _MARKER,
                AuditEventRow.result == "ok",
                (CorporateAction.ex_date > action.ex_date)
                | (
                    (CorporateAction.ex_date == action.ex_date)
                    & (CorporateAction.action_type == "CASH_DIVIDEND")
                    & (action.action_type == "SPLIT")
                ),
            )
            .limit(1)
        )
        if later_applied is not None:
            raise LedgerInvariantError(
                f"out-of-order corporate action {action.id} precedes {later_applied}"
            )
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
        if action.action_type == "SPLIT" and position is not None and position.qty:
            if action.ratio_from is None or action.ratio_to is None:
                raise LedgerInvariantError("split missing ratio")
            step = broker.quantity_step(symbol)
            split_position(
                position.qty, position.average_cost, action.ratio_from, action.ratio_to, step
            )
            for owner in load_holdings(session, account_id, now):
                if owner.symbol == symbol:
                    split_position(owner.qty, owner.avg_cost, action.ratio_from, action.ratio_to)
        broker.apply_action(
            session,
            action=action,
            symbol=symbol,
            expected_qty=position.qty if position else Decimal(0),
        )
        apply_corporate_action(session, account_id=account_id, action=action, now=now)
