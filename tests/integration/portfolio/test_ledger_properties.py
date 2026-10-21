"""Property test: equity always equals starting cash plus realized plus unrealized P&L."""

from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
from trading.core.config import load_config
from trading.portfolio.ledger import apply_fill
from trading.portfolio.valuation import value_portfolio

CFG = load_config(Path("configs")).paper_broker

operations = st.lists(
    st.tuples(
        st.sampled_from(["BUY", "SELL"]),
        st.integers(min_value=1, max_value=10),  # tenths of a share
        st.integers(min_value=9000, max_value=11000),  # price in cents
        st.integers(min_value=0, max_value=5),  # fee in cents
    ),
    min_size=1,
    max_size=8,
)


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(ops=operations)
def test_equity_identity(db: Session, ops: list[tuple[str, int, int, int]]) -> None:
    with db.begin_nested() as savepoint:
        account_id = open_account(db)
        held = D(0)
        applied = 0
        for n, (side, tenths, cents, fee_cents) in enumerate(ops):
            qty = Decimal(tenths) / 10
            if side == "SELL" and qty > held:
                continue
            order = new_order(
                db,
                account_id,
                f"o{n}",
                side=side,
                qty=str(qty),
                limit="500" if side == "BUY" else "1",
            )
            apply_fill(db, account_id=account_id,
                       fill=broker_fill(order, f"e{n}", qty=str(qty), price=str(Decimal(cents) / 100), fees=str(Decimal(fee_cents) / 100)),
                       settles_on=NEXT_DAY, now=T0)  # fmt: skip
            held += qty if side == "BUY" else -qty
            applied += 1
        state = value_portfolio(db, account_id, marks={"SPY": D("100")}, ask_by_symbol={}, as_of=T0,
                                session_date=DAY, cfg=CFG)  # fmt: skip
        drift = abs((state.equity - D("1000")) - (state.realized_pnl + state.unrealized_pnl))
        assert drift <= Decimal("0.0001") * max(applied, 1)
        assert state.equity == state.cash + state.gross_exposure
        savepoint.rollback()
