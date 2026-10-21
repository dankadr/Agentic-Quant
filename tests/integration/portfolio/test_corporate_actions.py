from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import (
    DAY,
    NEXT_DAY,
    D,
    broker_fill,
    new_order,
    open_account,
)
from trading.db.models.market import CorporateAction
from trading.db.models.portfolio import Position
from trading.portfolio.balances import cash_balances
from trading.portfolio.corporate_actions import apply_corporate_action
from trading.portfolio.ledger import apply_fill


def _holding(db: Session):  # type: ignore[no-untyped-def]
    account_id = open_account(db)
    order = new_order(db, account_id, "b1", qty="10", limit="500")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(order, "e-1", qty="10", price="50"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    return account_id, order.instrument_id


def _action(db: Session, instrument_id, **kw):  # type: ignore[no-untyped-def]
    defaults = {"ratio_from": None, "ratio_to": None, "cash_amount": None}
    row = CorporateAction(
        instrument_id=instrument_id, ex_date=DAY, provider="p", announced_at=T0, ingested_at=T0,
        **{**defaults, **kw},
    )  # fmt: skip
    db.add(row)
    db.flush()
    return row


def test_split_doubles_quantity_halves_cost_and_is_idempotent(db: Session) -> None:
    account_id, instrument_id = _holding(db)
    split = _action(db, instrument_id, action_type="SPLIT", ratio_from=D(1), ratio_to=D(2))
    assert apply_corporate_action(db, account_id=account_id, action=split, now=T0) is True
    pos = db.scalars(select(Position)).one()
    assert (pos.qty, pos.average_cost) == (D("20"), D("25.00000000"))
    assert apply_corporate_action(db, account_id=account_id, action=split, now=T0) is False
    db.refresh(pos)
    assert pos.qty == D("20")
    assert (
        db.execute(
            text("select count(*) from audit_events where action = 'corporate_action.applied'")
        ).scalar_one()
        == 1
    )


def test_reverse_split(db: Session) -> None:
    account_id, instrument_id = _holding(db)
    split = _action(db, instrument_id, action_type="SPLIT", ratio_from=D(4), ratio_to=D(1))
    apply_corporate_action(db, account_id=account_id, action=split, now=T0)
    pos = db.scalars(select(Position)).one()
    assert (pos.qty, pos.average_cost) == (D("2.5"), D("200.00000000"))


def test_dividend_credited_once_and_counted_as_realized(db: Session) -> None:
    account_id, instrument_id = _holding(db)
    dividend = _action(db, instrument_id, action_type="CASH_DIVIDEND", cash_amount=D("0.50"))
    before = cash_balances(db, account_id, as_of_date=DAY).total
    assert apply_corporate_action(db, account_id=account_id, action=dividend, now=T0) is True
    assert apply_corporate_action(db, account_id=account_id, action=dividend, now=T0) is False
    assert cash_balances(db, account_id, as_of_date=DAY).total - before == D("5.0000")
    pos = db.scalars(select(Position)).one()
    assert pos.realized_pnl == D("5.0000")
    entry = db.execute(
        text("select source_type, amount from cash_ledger where source_type = 'DIVIDEND'")
    ).one()
    assert tuple(entry) == ("DIVIDEND", D("5.0000"))


def test_action_for_unheld_instrument_is_processed_as_noop(db: Session) -> None:
    from tests.integration.portfolio.helpers import get_instrument

    account_id = open_account(db)
    inst = get_instrument(db, "QQQ")
    dividend = _action(db, inst.id, action_type="CASH_DIVIDEND", cash_amount=D("1"))
    assert apply_corporate_action(db, account_id=account_id, action=dividend, now=T0) is True
    assert cash_balances(db, account_id, as_of_date=DAY).total == D("1000.0000")
