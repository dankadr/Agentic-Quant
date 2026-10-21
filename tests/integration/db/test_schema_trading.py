from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

import trading.db.models  # noqa: F401
from tests.factories import (
    T0,
    full_chain,
    make_account,
    make_fill,
    make_instrument,
    make_intent,
    make_order,
    make_risk_decision,
    make_snapshot,
)
from trading.db.base import Base
from trading.db.models.market import CorporateAction
from trading.db.models.portfolio import CashLedgerEntry, Position
from trading.db.models.trading import OrderIntent, RiskDecisionRow

TRADING_TABLES = {
    "accounts", "instruments", "market_bars", "market_quotes", "market_snapshots",
    "corporate_actions", "strategies", "signals", "allocation_decisions", "agent_runs",
    "risk_decisions", "order_intents", "orders", "order_events", "fills", "cash_ledger",
    "positions", "strategy_attribution", "equity_snapshots", "trading_sessions",
    "reconciliation_runs",
}  # fmt: skip
VENUE_TABLES = {"venue_accounts", "venue_orders", "venue_fills"}


def test_trading_tables_exist(db: Session) -> None:
    insp = inspect(db.get_bind())
    assert TRADING_TABLES <= set(insp.get_table_names())
    assert VENUE_TABLES <= set(insp.get_table_names(schema="paper_venue"))


def test_orm_columns_match_database_for_every_table(db: Session) -> None:
    insp = inspect(db.get_bind())
    for table in Base.metadata.tables.values():
        db_cols = {c["name"] for c in insp.get_columns(table.name, schema=table.schema)}
        assert db_cols == set(table.columns.keys()), table.fullname


def test_account_mode_live_rejected(db: Session) -> None:
    account = make_account(db)
    with pytest.raises(IntegrityError):
        db.execute(text("update accounts set mode = 'LIVE' where id = :i"), {"i": account.id})


def test_asset_type_option_rejected(db: Session) -> None:
    inst = make_instrument(db)
    for bad in ("OPTION", "CRYPTO"):
        with pytest.raises(IntegrityError):
            db.execute(
                text("update instruments set asset_type = :a where id = :i"),
                {"a": bad, "i": inst.id},
            )
        db.rollback()
        make_instrument(db)  # session was reset by rollback; recreate for next loop
        break


def test_order_intent_requires_risk_decision_and_snapshot(db: Session) -> None:
    account = make_account(db)
    inst = make_instrument(db)
    decision = make_risk_decision(db, account, inst, make_snapshot(db))
    for column, n in (("risk_decision_id", 1), ("market_snapshot_id", 2)):
        intent = OrderIntent(
            account_id=account.id, instrument_id=inst.id, side="BUY", quantity=Decimal(1),
            order_type="MARKET", limit_price=None, time_in_force="DAY", strategy_ids=["s"],
            market_snapshot_id=decision.market_snapshot_id, risk_decision_id=decision.id,
            client_order_id=f"c-{n}", expires_at=None, is_entry=True, created_at=T0,
        )  # fmt: skip
        setattr(intent, column, None)
        db.add(intent)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        account = make_account(db)
        inst = make_instrument(db)
        decision = make_risk_decision(db, account, inst, make_snapshot(db))


def test_one_intent_per_risk_decision(db: Session) -> None:
    account, inst, decision, _, _ = full_chain(db)
    with pytest.raises(IntegrityError):
        make_intent(db, account, inst, decision, client_order_id="other-id")


def test_client_order_id_unique(db: Session) -> None:
    account, inst, decision, _, _ = full_chain(db)
    snap = make_snapshot(db, data_hash="h2")
    d2 = make_risk_decision(db, account, inst, snap, dedupe_key="k2")
    with pytest.raises(IntegrityError):
        make_intent(db, account, inst, d2)  # same client_order_id as the first intent


def test_limit_order_requires_price(db: Session) -> None:
    account, inst, decision, intent, _ = full_chain(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text("update orders set limit_price = NULL where intent_id = :i"), {"i": intent.id}
        )


def test_one_order_per_intent(db: Session) -> None:
    _, _, _, intent, _ = full_chain(db)
    with pytest.raises(IntegrityError):
        make_order(db, intent)


def test_filled_qty_cannot_exceed_qty(db: Session) -> None:
    _, _, _, _, order = full_chain(db)
    with pytest.raises(IntegrityError):
        db.execute(
            text("update orders set filled_qty = qty + 0.0001 where id = :i"), {"i": order.id}
        )


def test_order_status_check(db: Session) -> None:
    _, _, _, _, order = full_chain(db)
    with pytest.raises(IntegrityError):
        db.execute(text("update orders set status = 'BOGUS' where id = :i"), {"i": order.id})


def test_negative_position_rejected(db: Session) -> None:
    account = make_account(db)
    inst = make_instrument(db)
    db.add(Position(account_id=account.id, instrument_id=inst.id, qty=Decimal("-1"),
                    average_cost=Decimal(1), realized_pnl=Decimal(0), updated_at=T0))  # fmt: skip
    with pytest.raises(IntegrityError):
        db.flush()


def test_execution_id_unique(db: Session) -> None:
    _, _, _, _, order = full_chain(db)
    make_fill(db, order, execution_id="e-1", qty="0.05")
    with pytest.raises(IntegrityError):
        make_fill(db, order, execution_id="e-1", qty="0.05")


def test_fill_qty_and_price_must_be_positive(db: Session) -> None:
    _, _, _, _, order = full_chain(db)
    with pytest.raises(IntegrityError):
        make_fill(db, order, execution_id="e-0", qty="0")


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("fills", "update fills set qty = qty"),
        ("fills", "delete from fills"),
        ("cash_ledger", "update cash_ledger set amount = 0"),
        ("cash_ledger", "delete from cash_ledger"),
        ("order_intents", "update order_intents set quantity = quantity"),
        ("order_intents", "delete from order_intents"),
        ("risk_decisions", "update risk_decisions set allowed = allowed"),
        ("risk_decisions", "delete from risk_decisions"),
        ("order_events", "delete from order_events"),
        ("paper_venue.venue_fills", "delete from paper_venue.venue_fills"),
    ],
)
def test_immutable_tables_reject_update_and_delete(db: Session, table: str, sql: str) -> None:
    _, _, _, _, order = full_chain(db)
    make_fill(db, order)
    db.add(CashLedgerEntry(account_id=order.account_id, ts=T0, amount=Decimal("1.0000"), currency="USD",
                           source_type="ADJUSTMENT", source_id=None, settles_on=T0.date(), reverses_id=None))  # fmt: skip
    db.execute(
        text("insert into order_events(order_id, ts, to_status) values (:o, now(), 'CREATED')"),
        {"o": order.id},
    )
    db.execute(
        text("insert into paper_venue.venue_accounts(account_ref, starting_cash) values ('a', 1)")
    )
    db.execute(text(
        "insert into paper_venue.venue_orders(client_order_id, broker_order_id, account_ref, symbol, side, type,"
        " tif, qty, status, submitted_at, eligible_at, updated_at) values ('c','b','a','SPY','BUY','MARKET','DAY',1,"
        "'ACCEPTED', now(), now(), now())"))  # fmt: skip
    db.execute(text(
        "insert into paper_venue.venue_fills(execution_id, client_order_id, account_ref, symbol, side, ts, qty,"
        " price, fees, slippage_estimate) values ('x','c','a','SPY','BUY', now(), 1, 1, 0, 0)"))  # fmt: skip
    db.flush()
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text(sql))


def test_cash_ledger_source_type_check(db: Session) -> None:
    account = make_account(db)
    db.add(CashLedgerEntry(account_id=account.id, ts=T0, amount=Decimal(1), currency="USD",
                           source_type="MAGIC", source_id=None, settles_on=T0.date(), reverses_id=None))  # fmt: skip
    with pytest.raises(IntegrityError):
        db.flush()


def test_allowed_dedupe_key_unique_but_denied_may_repeat(db: Session) -> None:
    account = make_account(db)
    inst = make_instrument(db)
    snap = make_snapshot(db)
    make_risk_decision(db, account, inst, snap, allowed=False, dedupe_key="same")
    make_risk_decision(db, account, inst, snap, allowed=False, dedupe_key="same")
    make_risk_decision(db, account, inst, snap, allowed=True, dedupe_key="same")
    with pytest.raises(IntegrityError):
        make_risk_decision(db, account, inst, snap, allowed=True, dedupe_key="same")


def test_risk_decision_requires_market_snapshot(db: Session) -> None:
    account = make_account(db)
    inst = make_instrument(db)
    snap = make_snapshot(db)
    decision = make_risk_decision(db, account, inst, snap)
    db.expunge(decision)
    with pytest.raises(IntegrityError):
        make_risk_decision(db, account, inst, snap).market_snapshot_id = None
        db.add(RiskDecisionRow(account_id=account.id, instrument_id=inst.id, ts=T0, allowed=False,
                               reasons_json=[], limits_version="v", market_snapshot_id=None,
                               dedupe_key="n", proposal_json={}))  # fmt: skip
        db.flush()


def test_limit_intent_requires_price(db: Session) -> None:
    account, inst, decision, intent, _ = full_chain(db)
    snap = make_snapshot(db, data_hash="h3")
    d2 = make_risk_decision(db, account, inst, snap, dedupe_key="k3")
    with pytest.raises(IntegrityError):
        db.add(OrderIntent(account_id=account.id, instrument_id=inst.id, side="BUY", quantity=Decimal(1),
                           order_type="LIMIT", limit_price=None, time_in_force="DAY", strategy_ids=["s"],
                           market_snapshot_id=snap.id, risk_decision_id=d2.id, client_order_id="c-lim",
                           expires_at=None, is_entry=True, created_at=T0))  # fmt: skip
        db.flush()


def test_split_requires_ratio_and_dividend_requires_amount(db: Session) -> None:
    inst = make_instrument(db)
    for kwargs in (
        {"action_type": "SPLIT", "ratio_from": None, "ratio_to": Decimal(2), "cash_amount": None},
        {"action_type": "CASH_DIVIDEND", "ratio_from": None, "ratio_to": None, "cash_amount": None},
    ):
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                db.add(CorporateAction(instrument_id=inst.id, ex_date=T0.date(), provider="p",
                                       announced_at=T0, ingested_at=T0, **kwargs))  # fmt: skip
                db.flush()
