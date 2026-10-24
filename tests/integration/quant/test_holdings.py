from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill, new_order, open_account
from tests.integration.quant.test_registry import CFG
from trading.portfolio.ledger import apply_fill
from trading.quant.holdings import load_holdings
from trading.quant.registry import seed_strategies

D = Decimal
DAY2 = T0 + timedelta(days=1)


def _trade(db: Session, acct, coid: str, side: str, qty: str, price: str, at) -> None:  # type: ignore[no-untyped-def]
    order = new_order(db, acct, coid, side=side, qty=qty, limit="200", strategy_ids=["orb_v1"])
    fill = replace(broker_fill(order, f"e-{coid}", qty=qty, price=price), ts=at)
    apply_fill(db, account_id=acct, fill=fill, settles_on=NEXT_DAY, now=at)


def test_a_new_round_trip_starts_a_fresh_holding(db: Session) -> None:
    """Review C1: yesterday's closed trade must not age or re-price today's entry."""
    seed_strategies(db, CFG, now=T0)
    acct = open_account(db)
    _trade(db, acct, "b1", "BUY", "0.8", "100", T0)
    _trade(db, acct, "s1", "SELL", "0.8", "101", T0 + timedelta(hours=1))
    assert load_holdings(db, acct, DAY2) == ()
    _trade(db, acct, "b2", "BUY", "0.5", "110", DAY2)
    (h,) = load_holdings(db, acct, DAY2 + timedelta(minutes=1))
    assert (h.qty, h.avg_cost, h.opened_at) == (D("0.5"), D("110"), DAY2)


def test_a_rounding_residue_is_not_a_holding(db: Session) -> None:
    """Review C1 (dust): a sub-increment residue must not block re-entry forever."""
    seed_strategies(db, CFG, now=T0)
    acct = open_account(db)
    _trade(db, acct, "b1", "BUY", "1.0", "100", T0)
    _trade(db, acct, "s1", "SELL", "0.99996667", "101", T0 + timedelta(hours=1))
    assert load_holdings(db, acct, DAY2) == ()


def test_resting_buys_are_summed_per_strategy(db: Session) -> None:
    from trading.quant.holdings import load_open_buy_by_strategy

    seed_strategies(db, CFG, now=T0)
    acct = open_account(db)
    new_order(db, acct, "r1", qty="1.0", limit="100.00", strategy_ids=["orb_v1"])
    new_order(db, acct, "r2", qty="1.0", limit="50.00", strategy_ids=["orb_v1", "vwap_v1"])
    new_order(db, acct, "done", qty="1.0", limit="999", strategy_ids=["orb_v1"], status="FILLED")
    assert load_open_buy_by_strategy(db, acct) == {"orb_v1": D("125"), "vwap_v1": D("25")}
