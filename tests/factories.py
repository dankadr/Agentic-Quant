"""Row factories shared by integration tests. They flush but never commit."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from trading.db.models.market import Instrument, MarketSnapshotRow
from trading.db.models.portfolio import Account
from trading.db.models.trading import FillRow, Order, OrderIntent, RiskDecisionRow

T0 = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


def make_account(db: Session, name: str = "paper-primary", now: datetime = T0) -> Account:
    row = Account(
        name=name,
        broker="paper",
        mode="PAPER",
        currency="USD",
        status="ACTIVE",
        peak_equity=Decimal("1000.0000"),
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def make_instrument(db: Session, symbol: str = "SPY", fractional: bool = True) -> Instrument:
    row = Instrument(
        symbol=symbol,
        venue="XNYS",
        asset_type="ETF",
        tick_size=Decimal("0.01"),
        lot_size=Decimal(1),
        fractional_eligible=fractional,
        sector="Technology" if symbol in ("AAPL", "MSFT") else "Broad Market",
        active=True,
    )
    db.add(row)
    db.flush()
    return row


def make_snapshot(db: Session, now: datetime = T0, data_hash: str = "h") -> MarketSnapshotRow:
    row = MarketSnapshotRow(
        as_of=now, provider="replay", data_hash=data_hash, quality_status="OK", content_json={}
    )
    db.add(row)
    db.flush()
    return row


def make_risk_decision(
    db: Session,
    account: Account,
    instrument: Instrument,
    snapshot: MarketSnapshotRow,
    *,
    allowed: bool = True,
    dedupe_key: str = "k1",
    now: datetime = T0,
    proposal_json: dict | None = None,  # type: ignore[type-arg]
) -> RiskDecisionRow:
    row = RiskDecisionRow(
        signal_id=None,
        account_id=account.id,
        instrument_id=instrument.id,
        ts=now,
        allowed=allowed,
        reasons_json=[],
        limits_version="v1",
        market_snapshot_id=snapshot.id,
        portfolio_snapshot_id=None,
        dedupe_key=dedupe_key,
        proposal_json=proposal_json or {},
    )
    db.add(row)
    db.flush()
    return row


def make_intent(
    db: Session,
    account: Account,
    instrument: Instrument,
    decision: RiskDecisionRow,
    *,
    client_order_id: str = "paper-orb_v1-20261019-0001",
    quantity: str = "0.15",
    side: str = "BUY",
    limit_price: str | None = "500.00",
    strategy_ids: list[str] | None = None,
    now: datetime = T0,
) -> OrderIntent:
    row = OrderIntent(
        account_id=account.id,
        instrument_id=instrument.id,
        side=side,
        quantity=Decimal(quantity),
        order_type="LIMIT" if limit_price else "MARKET",
        limit_price=Decimal(limit_price) if limit_price else None,
        time_in_force="DAY",
        strategy_ids=strategy_ids or ["orb_v1"],
        market_snapshot_id=decision.market_snapshot_id,
        risk_decision_id=decision.id,
        client_order_id=client_order_id,
        expires_at=None,
        is_entry=True,
        created_at=now,
    )
    db.add(row)
    db.flush()
    return row


def make_order(
    db: Session, intent: OrderIntent, *, status: str = "CREATED", now: datetime = T0
) -> Order:
    row = Order(
        account_id=intent.account_id,
        intent_id=intent.id,
        instrument_id=intent.instrument_id,
        client_order_id=intent.client_order_id,
        broker_order_id=None,
        side=intent.side,
        type=intent.order_type,
        time_in_force=intent.time_in_force,
        limit_price=intent.limit_price,
        qty=intent.quantity,
        filled_qty=Decimal(0),
        avg_fill_price=None,
        status=status,
        status_reason=None,
        version=1,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def make_fill(
    db: Session, order: Order, *, execution_id: str = "e-1", qty: str = "0.1", now: datetime = T0
) -> FillRow:
    row = FillRow(
        order_id=order.id,
        execution_id=execution_id,
        ts=now,
        qty=Decimal(qty),
        price=Decimal("500.02"),
        fees=Decimal("0.0000"),
        slippage_estimate=Decimal("0.0100"),
    )
    db.add(row)
    db.flush()
    return row


def full_chain(db: Session) -> tuple[Account, Instrument, RiskDecisionRow, OrderIntent, Order]:
    account = make_account(db)
    instrument = make_instrument(db)
    snapshot = make_snapshot(db)
    decision = make_risk_decision(db, account, instrument, snapshot)
    intent = make_intent(db, account, instrument, decision)
    order = make_order(db, intent)
    return account, instrument, decision, intent, order


__all__ = ["T0", "UUID", "full_chain", "make_account", "make_fill", "make_instrument",
           "make_intent", "make_order", "make_risk_decision", "make_snapshot"]  # fmt: skip
