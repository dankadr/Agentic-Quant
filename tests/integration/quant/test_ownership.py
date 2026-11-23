from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill, new_order, open_account
from tests.integration.quant.test_registry import CFG
from trading.portfolio.ledger import apply_fill, open_paper_account
from trading.quant.holdings import load_holdings
from trading.quant.ownership import load_symbol_owners
from trading.quant.registry import seed_strategies


def test_pending_order_reserves_ticker_across_sessions_and_restart(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    account = open_account(db)
    new_order(db, account, "pending", strategy_ids=["vwap_v1"])
    db.commit()
    with Session(db.bind) as restarted:
        assert load_symbol_owners(restarted, account, T0 + timedelta(days=1)) == {
            "SPY": frozenset({"vwap_v1"})
        }


@pytest.mark.parametrize("status", ["CREATED", "SUBMITTING", "PARTIALLY_FILLED", "UNKNOWN"])
def test_unresolved_order_keeps_ownership(db: Session, status: str) -> None:
    account = open_account(db)
    new_order(db, account, "pending", status=status, strategy_ids=["orb_v1"])
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"orb_v1"})}


@pytest.mark.parametrize("status", ["CANCELED", "REJECTED", "EXPIRED"])
def test_terminal_unfilled_order_releases_ticker(db: Session, status: str) -> None:
    account = open_account(db)
    new_order(db, account, "done", status=status, strategy_ids=["orb_v1"])
    assert load_symbol_owners(db, account, T0) == {}


def test_partial_fill_then_cancellation_keeps_owner_until_full_exit(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    account = open_account(db)
    buy = new_order(db, account, "buy", qty="1", strategy_ids=["vwap_v1"])
    apply_fill(
        db,
        account_id=account,
        fill=broker_fill(buy, "b", qty="0.5", price="100"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    buy.status = "CANCELED"
    db.flush()
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"vwap_v1"})}
    sell = new_order(db, account, "sell", side="SELL", qty="0.5", strategy_ids=["vwap_v1"])
    fill = broker_fill(sell, "s1", qty="0.2", price="101")
    apply_fill(db, account_id=account, fill=fill, settles_on=NEXT_DAY, now=T0)
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"vwap_v1"})}
    apply_fill(
        db,
        account_id=account,
        fill=replace(fill, execution_id="s2", qty=Decimal("0.3")),
        settles_on=NEXT_DAY,
        now=T0,
    )
    # Even when flat, an unresolved order must retain the reservation.
    sell.status = "UNKNOWN"
    db.flush()
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"vwap_v1"})}
    sell.status = "FILLED"
    db.flush()
    assert load_symbol_owners(db, account, T0) == {}


def test_ownership_is_account_scoped(db: Session) -> None:
    first = open_account(db)
    second = open_paper_account(db, name="other", starting_cash=Decimal("1000"), now=T0)
    new_order(db, first, "pending", strategy_ids=["orb_v1"])
    assert load_symbol_owners(db, second, T0) == {}


def test_partly_unattributed_position_keeps_an_unknown_owner_reservation(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    account = open_account(db)
    for strategy, qty in (("orb_v1", "0.4"), ("unknown_strategy", "0.1")):
        order = new_order(db, account, strategy, qty=qty, strategy_ids=[strategy])
        apply_fill(
            db,
            account_id=account,
            fill=broker_fill(order, strategy, qty=qty, price="100"),
            settles_on=NEXT_DAY,
            now=T0,
        )
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"orb_v1", ""})}


def test_completed_safety_exit_does_not_reappear_as_owner_on_the_next_trade(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    account = open_account(db)
    # Timestamps deliberately tie; the durable fill event sequence determines actual order.
    trades = [
        ("unknown_strategy", "BUY", "0.1"),
        ("eod_flatten", "SELL", "0.1"),
        ("vwap_v1", "BUY", "0.5"),
    ]
    for index, (strategy, side, qty) in enumerate(trades):
        order = new_order(db, account, str(index), side=side, qty=qty, strategy_ids=[strategy])
        apply_fill(
            db,
            account_id=account,
            fill=broker_fill(order, str(index), qty=qty, price="100"),
            settles_on=NEXT_DAY,
            now=T0,
        )
        if index == 1:
            assert load_symbol_owners(db, account, T0) == {}
    assert load_symbol_owners(db, account, T0) == {"SPY": frozenset({"vwap_v1"})}
    assert [(h.strategy_id, h.qty) for h in load_holdings(db, account, T0)] == [
        ("vwap_v1", Decimal("0.5"))
    ]
