from decimal import Decimal
from pathlib import Path

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
from trading.core.config import load_config
from trading.core.errors import TradingError
from trading.portfolio.balances import reserved_for_open_buys
from trading.portfolio.ledger import apply_fill
from trading.portfolio.valuation import MissingMark, record_equity_snapshot, value_portfolio

CFG = load_config(Path("configs")).paper_broker


def _hold(db: Session, symbol: str = "SPY", qty: str = "1", price: str = "100", coid: str = "b1"):  # type: ignore[no-untyped-def]
    account_id = (
        open_account(db)
        if not db.execute(text("select 1 from accounts")).first()
        else db.execute(text("select id from accounts")).scalar_one()
    )
    order = new_order(db, account_id, coid, qty=qty, limit="500", symbol=symbol)
    apply_fill(db, account_id=account_id, fill=broker_fill(order, f"e-{coid}", qty=qty, price=price, symbol=symbol),
               settles_on=NEXT_DAY, now=T0)  # fmt: skip
    return account_id


def _value(db: Session, account_id, marks: dict[str, str], asks: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    return value_portfolio(db, account_id, marks={k: D(v) for k, v in marks.items()},
                           ask_by_symbol={k: D(v) for k, v in (asks or {}).items()},
                           as_of=T0, session_date=DAY, cfg=CFG)  # fmt: skip


def test_buying_power_subtracts_open_buy_reservations(db: Session) -> None:
    account_id = open_account(db)
    new_order(db, account_id, "o1", qty="2", limit="100.00")
    assert reserved_for_open_buys(db, account_id, ask_by_symbol={}, cfg=CFG) == D("200.0000")
    state = _value(db, account_id, {})
    assert (state.cash, state.settled_cash, state.reserved_cash, state.buying_power) == (
        D("1000.0000"), D("1000.0000"), D("200.0000"), D("800.0000"),
    )  # fmt: skip


def test_partially_filled_buy_reserves_only_remaining(db: Session) -> None:
    account_id = open_account(db)
    order = new_order(db, account_id, "o1", qty="2", limit="100.00")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(order, "e-1", qty="0.5", price="99"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    assert reserved_for_open_buys(db, account_id, ask_by_symbol={}, cfg=CFG) == D("150.0000")


def test_terminal_and_sell_orders_reserve_nothing(db: Session) -> None:
    account_id = open_account(db)
    new_order(db, account_id, "o1", qty="2", limit="100.00", status="REJECTED")
    new_order(db, account_id, "o2", qty="2", limit="100.00", status="CANCELED")
    new_order(db, account_id, "o3", side="SELL", qty="2", limit="100.00")
    assert reserved_for_open_buys(db, account_id, ask_by_symbol={}, cfg=CFG) == D("0")


def test_market_buy_reservation_uses_ask_and_requires_it(db: Session) -> None:
    account_id = open_account(db)
    new_order(db, account_id, "m1", qty="2", limit=None)
    assert reserved_for_open_buys(db, account_id, ask_by_symbol={"SPY": D("100.50")}, cfg=CFG) == D(
        "201.0000"
    )
    with pytest.raises(MissingMark):
        reserved_for_open_buys(db, account_id, ask_by_symbol={}, cfg=CFG)


def test_reservation_includes_estimated_fees(db: Session) -> None:
    account_id = open_account(db)
    new_order(db, account_id, "o1", qty="1", limit="100.00")
    cfg = CFG.model_copy(update={"commission_per_order_usd": D("0.50")})
    assert reserved_for_open_buys(db, account_id, ask_by_symbol={}, cfg=cfg) == D("100.5000")


def test_value_portfolio_exposures_and_pnl(db: Session) -> None:
    account_id = _hold(db, "SPY", "1", "100", "b1")
    _hold(db, "AAPL", "0.5", "200", "b2")
    state = _value(db, account_id, {"SPY": "102", "AAPL": "190"})
    assert state.cash == D("800.0000")
    assert state.gross_exposure == D("197.0000")  # 102 + 95
    assert state.equity == D("997.0000")
    assert state.unrealized_pnl == D("-3.0000")  # +2 on SPY, -5 on AAPL
    assert state.realized_pnl == D("0.0000")
    assert state.exposure_by_symbol == {"SPY": D("102.0000"), "AAPL": D("95.0000")}
    assert sum(state.exposure_by_sector.values()) == D("197.0000")
    by_symbol = {p.symbol: p for p in state.positions}
    assert by_symbol["SPY"].unrealized_pnl == D("2.0000")
    assert by_symbol["AAPL"].market_value == D("95.0000")


def test_value_portfolio_missing_mark_raises(db: Session) -> None:
    account_id = _hold(db)
    with pytest.raises(MissingMark, match="SPY"):
        _value(db, account_id, {})
    assert issubclass(MissingMark, TradingError)


def test_flat_account_needs_no_marks(db: Session) -> None:
    account_id = open_account(db)
    state = _value(db, account_id, {})
    assert (state.equity, state.gross_exposure, state.positions) == (D("1000.0000"), D("0"), ())


def test_closed_position_keeps_realized_pnl_without_needing_a_mark(db: Session) -> None:
    account_id = _hold(db, "SPY", "1", "100", "b1")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(sell, "e-s1", qty="1", price="103"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    state = _value(db, account_id, {})
    assert state.realized_pnl == D("3.0000")
    assert state.equity == D("1003.0000")
    assert state.positions == ()


def test_unsettled_proceeds_excluded_from_buying_power(db: Session) -> None:
    account_id = _hold(db, "SPY", "1", "100", "b1")
    sell = new_order(db, account_id, "s1", side="SELL", qty="1", limit="50")
    apply_fill(
        db,
        account_id=account_id,
        fill=broker_fill(sell, "e-s1", qty="1", price="100"),
        settles_on=NEXT_DAY,
        now=T0,
    )
    state = _value(db, account_id, {})
    assert state.cash == D("1000.0000")
    assert state.settled_cash == D("900.0000")
    assert state.buying_power == D("900.0000")


def test_record_equity_snapshot_persists_state(db: Session) -> None:
    account_id = _hold(db)
    state = _value(db, account_id, {"SPY": "101"})
    snapshot_id = record_equity_snapshot(db, state, kind="SESSION_START")
    row = db.execute(
        text(
            "select kind, cash, equity, exposure, unrealized_pnl, positions_json from equity_snapshots where id = :i"
        ),
        {"i": snapshot_id},
    ).one()
    assert (row.kind, row.cash, row.equity, row.exposure, row.unrealized_pnl) == (
        "SESSION_START", D("900.0000"), D("1001.0000"), D("101.0000"), D("1.0000"),
    )  # fmt: skip
    assert row.positions_json["SPY"]["qty"] == "1"


def test_instrument_helper(db: Session) -> None:
    assert get_instrument(db, "SPY").symbol == "SPY"
    assert Decimal("1") == D("1")
