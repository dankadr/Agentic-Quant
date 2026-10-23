from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import (
    DAY,
    NEXT_DAY,
    D,
    broker_fill,
    get_instrument,
    new_order,
    open_account,
)
from trading.core.enums import Side
from trading.execution.state_machine import IllegalTransition, OrderStatus
from trading.portfolio.balances import cash_balances
from trading.portfolio.ledger import (
    LedgerInvariantError,
    apply_fill,
    open_paper_account,
    reverse_ledger_entry,
)


def _one(db: Session, sql: str) -> object:
    return db.execute(text(sql)).scalar_one()


def _position(db: Session) -> tuple[Decimal, Decimal, Decimal]:
    row = db.execute(text("select qty, average_cost, realized_pnl from positions")).one()
    return row.qty, row.average_cost, row.realized_pnl


def test_open_paper_account_deposits_starting_cash(db: Session) -> None:
    account_id = open_account(db)
    row = db.execute(text("select mode, currency, peak_equity from accounts")).one()
    assert tuple(row) == ("PAPER", "USD", D("1000.0000"))
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("1000.0000")
    assert _one(db, "select source_type from cash_ledger") == "INITIAL_DEPOSIT"


def test_partial_fills_update_cash_position_fees_remaining(db: Session) -> None:
    """Acceptance test 7."""
    account_id = open_account(db)
    order = new_order(db, account_id, "o1", qty="1.0", limit="100.05")
    a1 = apply_fill(db, account_id=account_id, fill=broker_fill(order, "e-1", qty="0.4", price="100.02"),
                    settles_on=NEXT_DAY, now=T0)  # fmt: skip
    assert a1 is not None
    assert a1.order_status is OrderStatus.PARTIALLY_FILLED
    assert a1.remaining_qty == D("0.6")
    assert a1.position_qty == D("0.4")
    a2 = apply_fill(db, account_id=account_id,
                    fill=broker_fill(order, "e-2", qty="0.6", price="100.03", fees="0.01"),
                    settles_on=NEXT_DAY, now=T0 + timedelta(seconds=1))  # fmt: skip
    assert a2 is not None
    assert a2.order_status is OrderStatus.FILLED
    assert a2.remaining_qty == D("0")
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("899.9640")
    assert _position(db) == (D("1.0"), D("100.02600000"), D("-0.0100"))
    row = db.execute(text("select filled_qty, avg_fill_price, status, version from orders")).one()
    assert (row.filled_qty, row.avg_fill_price, row.status) == (
        D("1.0"),
        D("100.02600000"),
        "FILLED",
    )
    assert row.version == 3
    events = db.execute(text("select from_status, to_status from order_events order by seq")).all()
    assert [tuple(e) for e in events] == [
        ("ACKNOWLEDGED", "PARTIALLY_FILLED"),
        ("PARTIALLY_FILLED", "FILLED"),
    ]
    assert _one(db, "select count(*) from outbox where event_type = 'fill_applied'") == 2


def test_duplicate_execution_id_is_ignored(db: Session) -> None:
    account_id = open_account(db)
    order = new_order(db, account_id, "o1")
    fill = broker_fill(order, "e-1", qty="0.4", price="100.02")
    assert apply_fill(db, account_id=account_id, fill=fill, settles_on=NEXT_DAY, now=T0) is not None
    assert apply_fill(db, account_id=account_id, fill=fill, settles_on=NEXT_DAY, now=T0) is None
    assert _one(db, "select count(*) from fills") == 1
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("959.9920")
    assert _position(db)[0] == D("0.4")


def test_overfill_raises_and_writes_nothing(db: Session) -> None:
    account_id = open_account(db)
    order = new_order(db, account_id, "o1", qty="1.0")
    apply_fill(db, account_id=account_id, fill=broker_fill(order, "e-1", qty="0.9", price="100"),
               settles_on=NEXT_DAY, now=T0)  # fmt: skip
    with pytest.raises(LedgerInvariantError, match="overfill"):
        apply_fill(db, account_id=account_id, fill=broker_fill(order, "e-2", qty="0.2", price="100"),
                   settles_on=NEXT_DAY, now=T0)  # fmt: skip
    assert _one(db, "select count(*) from fills") == 1
    assert _position(db)[0] == D("0.9")


def test_oversell_raises(db: Session) -> None:
    account_id = open_account(db)
    order = new_order(db, account_id, "s1", side="SELL", qty="1.0", limit="99")
    with pytest.raises(LedgerInvariantError, match="oversell"):
        apply_fill(db, account_id=account_id, fill=broker_fill(order, "e-1", qty="1.0", price="100"),
                   settles_on=NEXT_DAY, now=T0)  # fmt: skip
    assert _one(db, "select count(*) from fills") == 0


def test_fill_for_unknown_order_raises(db: Session) -> None:
    account_id = open_account(db)
    order = new_order(db, account_id, "o1")
    fill = broker_fill(order, "e-1", qty="0.1", price="100")
    ghost = fill.__class__(**{**fill.__dict__, "client_order_id": "nope"})
    with pytest.raises(LedgerInvariantError, match="unknown order"):
        apply_fill(db, account_id=account_id, fill=ghost, settles_on=NEXT_DAY, now=T0)


@pytest.mark.parametrize(
    "status", ["CREATED", "REJECTED", "CANCELED", "EXPIRED", "FILLED", "RISK_APPROVED"]
)
def test_fills_on_orders_that_cannot_fill_have_no_effect(db: Session, status: str) -> None:
    """Rejected orders never affect positions."""
    account_id = open_account(db)
    order = new_order(db, account_id, "o1", status=status)
    with pytest.raises(IllegalTransition):
        apply_fill(db, account_id=account_id, fill=broker_fill(order, "e-1", qty="0.1", price="100"),
                   settles_on=NEXT_DAY, now=T0)  # fmt: skip
    assert _one(db, "select count(*) from fills") == 0
    assert _one(db, "select count(*) from positions") == 0
    assert _one(db, "select count(*) from cash_ledger") == 1  # only the initial deposit


def test_realized_pnl_round_trip(db: Session) -> None:
    account_id = open_account(db)
    buy = new_order(db, account_id, "b1", qty="1", limit="101")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(buy, "e-1", qty="1", price="100"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    apply_fill(db, account_id=account_id, fill=broker_fill(sell, "e-2", qty="1", price="101", fees="0.01"),
               settles_on=NEXT_DAY, now=T0)  # fmt: skip
    qty, _avg, realized = _position(db)
    assert (qty, realized) == (D("0"), D("0.9900"))
    assert cash_balances(db, account_id, as_of_date=NEXT_DAY).total == D("1000.9900")


def test_partial_sell_keeps_average_cost(db: Session) -> None:
    account_id = open_account(db)
    buy = new_order(db, account_id, "b1", qty="2", limit="101")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(buy, "e-1", qty="2", price="100"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(sell, "e-2", qty="1", price="103"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    assert _position(db) == (D("1"), D("100.00000000"), D("3.0000"))


def test_sale_proceeds_unsettled_until_t_plus_1(db: Session) -> None:
    account_id = open_account(db)
    buy = new_order(db, account_id, "b1", qty="1", limit="101")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(buy, "e-1", qty="1", price="100"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(sell, "e-2", qty="1", price="101"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    today = cash_balances(db, account_id, as_of_date=DAY)
    tomorrow = cash_balances(db, account_id, as_of_date=NEXT_DAY)
    assert (today.total, today.settled, today.unsettled) == (
        D("1001.0000"),
        D("900.0000"),
        D("101.0000"),
    )
    assert (tomorrow.settled, tomorrow.unsettled) == (D("1001.0000"), D("0.0000"))


def test_buy_debit_reduces_settled_cash_immediately(db: Session) -> None:
    account_id = open_account(db)
    buy = new_order(db, account_id, "b1", qty="1", limit="101")
    apply_fill(db, account_id=account_id, fill=broker_fill(buy, "e-1", qty="1", price="100", fees="0.02"),
               settles_on=NEXT_DAY, now=T0)  # fmt: skip
    assert cash_balances(db, account_id, as_of_date=DAY).settled == D("899.9800")


def test_reversal_entry_restores_balance_and_cannot_repeat(db: Session) -> None:
    account_id = open_account(db)
    db.execute(text("insert into cash_ledger(account_id, ts, amount, currency, source_type, settles_on)"
                    " values (:a, now(), -5.0000, 'USD', 'ADJUSTMENT', :d)"), {"a": account_id, "d": DAY})  # fmt: skip
    entry_id = _one(db, "select id from cash_ledger where source_type = 'ADJUSTMENT'")
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("995.0000")
    reversal = reverse_ledger_entry(db, entry_id=entry_id, reason="typo", now=T0)  # type: ignore[arg-type]
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("1000.0000")
    row = db.execute(
        text("select source_type, reverses_id from cash_ledger where id = :i"), {"i": reversal}
    ).one()
    assert (row.source_type, row.reverses_id) == ("REVERSAL", entry_id)
    with pytest.raises(LedgerInvariantError, match="already reversed"):
        reverse_ledger_entry(db, entry_id=entry_id, reason="again", now=T0)  # type: ignore[arg-type]
    with pytest.raises(LedgerInvariantError, match="reversal"):
        reverse_ledger_entry(db, entry_id=reversal, reason="undo undo", now=T0)
    assert _one(db, "select count(*) from audit_events where action = 'ledger.reverse'") == 1


def test_reverse_unknown_entry_raises(db: Session) -> None:
    from uuid import uuid4

    with pytest.raises(LedgerInvariantError, match="not found"):
        reverse_ledger_entry(db, entry_id=uuid4(), reason="x", now=T0)


def test_open_paper_account_name_is_unique(db: Session) -> None:
    from sqlalchemy.exc import IntegrityError

    open_account(db)
    with pytest.raises(IntegrityError):
        open_paper_account(db, name="paper-primary", starting_cash=D("1"), now=T0)


def test_side_enum_roundtrip(db: Session) -> None:
    assert Side("BUY") is Side.BUY
    assert get_instrument(db).symbol == "SPY"
