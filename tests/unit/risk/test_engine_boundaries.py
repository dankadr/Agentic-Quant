"""Property tests: the exposure and cash boundaries hold for arbitrary sizes and prices."""

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.unit.risk.helpers import evaluate, symbol_view
from trading.data.calendar import MarketCalendar

D = Decimal
SETTINGS = settings(
    max_examples=200, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


@SETTINGS
@given(
    qty_units=st.integers(min_value=1, max_value=50_000),  # 0.0001 .. 5 shares
    price_cents=st.integers(min_value=1_000, max_value=60_000),  # $10 .. $600
)
def test_empty_portfolio_entry_allowed_iff_within_symbol_cap(
    calendar: MarketCalendar, qty_units: int, price_cents: int
) -> None:
    qty = D(qty_units) / 10_000
    price = D(price_cents) / 100
    # quote tightly around the limit so the spread filter never interferes
    views = {"SPY": symbol_view("SPY", bid=str(price - D("0.01")), ask=str(price))}
    result = evaluate(calendar, qty=str(qty), limit=str(price), views=views)
    within_cap = qty * price <= D("100")  # 10% of $1,000 equity
    assert result.allowed is within_cap, (qty, price, [r.code for r in result.reasons])
    if not within_cap:
        assert any(r.detail == "symbol" for r in result.reasons)


@SETTINGS
@given(
    held_units=st.integers(min_value=0, max_value=9_000),  # existing SPY exposure in $0.10 steps
    add_qty_units=st.integers(min_value=1, max_value=2_000),
)
def test_existing_exposure_always_counts_toward_the_cap(
    calendar: MarketCalendar, held_units: int, add_qty_units: int
) -> None:
    from tests.unit.risk.helpers import portfolio

    held_value = D(held_units) / 10  # up to $900
    qty_held = held_value / 100
    positions = [("SPY", str(qty_held), "100")] if held_units else []
    pf = portfolio(positions=positions)
    add = D(add_qty_units) / 10_000
    result = evaluate(calendar, qty=str(add), limit="100.00", pf=pf)
    breach = held_value + add * 100 > D("100")
    assert any(r.detail == "symbol" for r in result.reasons) is breach
