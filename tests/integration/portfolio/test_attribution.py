from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.portfolio.helpers import NEXT_DAY, D, broker_fill, new_order, open_account
from trading.core.config import load_config
from trading.portfolio.ledger import apply_fill
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))


def setup(db: Session) -> object:
    seed_strategies(db, CFG, now=T0)
    return open_account(db)


def rows(db: Session) -> dict[str, tuple[Decimal, Decimal, Decimal | None]]:
    out = db.execute(
        text(
            "select strategy_id, sum(allocated_qty), sum(allocated_fees), sum(pnl) from strategy_attribution group by 1"
        )
    ).all()
    return {r[0]: (r[1], r[2], r[3]) for r in out}


def fill(db: Session, account_id, order, eid: str, qty: str, price: str, fees: str = "0", at=T0):  # type: ignore[no-untyped-def]
    return apply_fill(db, account_id=account_id, fill=broker_fill(order, eid, qty=qty, price=price, fees=fees),
                      settles_on=NEXT_DAY, now=at)  # fmt: skip


def test_single_strategy_fill_is_fully_attributed(db: Session) -> None:
    account_id = setup(db)
    order = new_order(db, account_id, "o1", qty="1", limit="101", strategy_ids=["orb_v1"])
    fill(db, account_id, order, "e1", "1", "100", fees="0.02")
    assert rows(db) == {
        "orb_v1": (D("1"), D("0.0200"), D("-0.0200"))
    }  # a buy: P&L is just its fees


def test_netted_order_splits_each_partial_fill_by_the_recorded_weights(db: Session) -> None:
    account_id = setup(db)
    order = new_order(db, account_id, "o1", qty="0.4992", limit="101", strategy_ids=["orb_v1", "vwap_v1"],
                      attribution={"orb_v1": "0.7992", "vwap_v1": "-0.3"})  # fmt: skip
    fill(db, account_id, order, "e1", "0.2496", "100")  # half the order
    assert rows(db)["orb_v1"][0] == D("0.3996")
    assert rows(db)["vwap_v1"][0] == D("-0.15")
    fill(db, account_id, order, "e2", "0.2496", "100", at=T0 + timedelta(seconds=1))
    done = rows(db)
    assert (done["orb_v1"][0], done["vwap_v1"][0]) == (D("0.7992"), D("-0.3"))
    assert done["orb_v1"][0] + done["vwap_v1"][0] == D(
        "0.4992"
    )  # attribution always sums to the filled shares


def test_attribution_sums_exactly_even_when_weights_do_not_divide_evenly(db: Session) -> None:
    account_id = setup(db)
    order = new_order(db, account_id, "o1", qty="1", limit="101", strategy_ids=["orb_v1", "vwap_v1"],
                      attribution={"orb_v1": "0.3333", "vwap_v1": "0.6667"})  # fmt: skip
    fill(db, account_id, order, "e1", "0.3", "100", fees="0.01")
    r = rows(db)
    assert r["orb_v1"][0] + r["vwap_v1"][0] == D("0.3")
    assert r["orb_v1"][1] + r["vwap_v1"][1] == D("0.0100")


def test_sell_fill_splits_realized_pnl_and_fees(db: Session) -> None:
    account_id = setup(db)
    buy = new_order(db, account_id, "b1", qty="1", limit="101", strategy_ids=["orb_v1"])
    fill(db, account_id, buy, "e1", "1", "100")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50", strategy_ids=["orb_v1", "vwap_v1"],
                     attribution={"orb_v1": "-0.5", "vwap_v1": "-0.5"})  # fmt: skip
    fill(db, account_id, sell, "e2", "1", "103", fees="0.02")
    r = rows(db)
    realized = db.execute(text("select realized_pnl from positions")).scalar_one()  # 3 - 0.02 fees
    assert realized == D("2.9800")
    assert r["orb_v1"][2] + r["vwap_v1"][2] + D("0") == D("2.9800") + D(
        "0"
    )  # buy fee was 0, so sells' P&L = realized
    assert r["orb_v1"][0] == D("0.5")  # bought 1, then sold half of the 1 share
    assert r["vwap_v1"][0] == D("-0.5")
    assert r["orb_v1"][1] + r["vwap_v1"][1] == D("0.02")


def test_orders_without_weights_split_equally(db: Session) -> None:
    account_id = setup(db)
    order = new_order(
        db, account_id, "o1", qty="1", limit="101", strategy_ids=["orb_v1", "vwap_v1"]
    )
    fill(db, account_id, order, "e1", "0.5", "100")
    r = rows(db)
    assert (r["orb_v1"][0], r["vwap_v1"][0]) == (D("0.25"), D("0.25"))


def test_unknown_strategy_never_blocks_the_ledger(db: Session) -> None:
    """Attribution is analytics; cash and positions are the truth and must always be recorded."""
    open_account(db)
    account_id = db.execute(
        text("select id from accounts")
    ).scalar_one()  # strategies deliberately NOT seeded
    order = new_order(db, account_id, "o1", qty="1", limit="101", strategy_ids=["orb_v1"])
    assert fill(db, account_id, order, "e1", "1", "100") is not None
    assert db.execute(text("select count(*) from fills")).scalar_one() == 1
    assert db.execute(text("select count(*) from strategy_attribution")).scalar_one() == 0


def test_duplicate_fill_does_not_duplicate_attribution(db: Session) -> None:
    account_id = setup(db)
    order = new_order(db, account_id, "o1", qty="1", limit="101", strategy_ids=["orb_v1"])
    fill(db, account_id, order, "e1", "0.5", "100")
    assert fill(db, account_id, order, "e1", "0.5", "100") is None
    assert db.execute(text("select count(*) from strategy_attribution")).scalar_one() == 1


def test_realized_pnl_goes_only_to_the_strategies_that_reduced_the_position(db: Session) -> None:
    """Review I4: on a net SELL the buying leg is an internal transfer and earns no realized P&L."""
    account_id = setup(db)
    buy = new_order(db, account_id, "b1", qty="1", limit="101", strategy_ids=["orb_v1"])
    fill(db, account_id, buy, "e1", "1", "100")
    sell = new_order(db, account_id, "s1", side="SELL", qty="0.6", limit="109", strategy_ids=["orb_v1", "vwap_v1"],
                     attribution={"orb_v1": "-1.0", "vwap_v1": "0.4"})  # fmt: skip
    fill(db, account_id, sell, "e2", "0.6", "110", fees="0.14")
    out = rows(db)
    total_gross = sum((v[2] + v[1] for k, v in out.items() if k in ("orb_v1", "vwap_v1")), D(0))
    assert total_gross == D("6.0000")  # all of the gross P&L, nothing invented or lost
    vwap = out["vwap_v1"]
    assert vwap[2] == -vwap[1]  # the transfer leg carries only its share of fees
