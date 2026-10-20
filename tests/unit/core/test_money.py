from decimal import Decimal

import pytest

from trading.core.enums import Side
from trading.core.money import (
    ceil_to_increment,
    floor_to_increment,
    is_multiple_of,
    quantize_cash,
    quantize_price,
    to_decimal,
    trade_cash_amount,
)


def test_to_decimal_rejects_float() -> None:
    with pytest.raises(TypeError):
        to_decimal(0.1)  # type: ignore[arg-type]


def test_to_decimal_accepts_str_int_decimal() -> None:
    assert to_decimal("1.50") == Decimal("1.50")
    assert to_decimal(2) == Decimal(2)
    assert to_decimal(Decimal("3")) == Decimal(3)


def test_to_decimal_rejects_bool() -> None:
    with pytest.raises(TypeError):
        to_decimal(True)


def test_floor_to_increment_never_rounds_up() -> None:
    assert floor_to_increment(Decimal(100) / Decimal("576.00"), Decimal("0.0001")) == Decimal(
        "0.1736"
    )
    assert floor_to_increment(Decimal("0.99999999"), Decimal(1)) == Decimal(0)


def test_ceil_to_increment() -> None:
    assert ceil_to_increment(Decimal("100.030002"), Decimal("0.01")) == Decimal("100.04")
    assert ceil_to_increment(Decimal("100.03"), Decimal("0.01")) == Decimal("100.03")


def test_is_multiple_of() -> None:
    assert is_multiple_of(Decimal("100.05"), Decimal("0.01"))
    assert not is_multiple_of(Decimal("100.005"), Decimal("0.01"))


def test_quantize_price_8dp_half_even() -> None:
    assert quantize_price(Decimal("1.000000005")) == Decimal("1.00000000")
    assert quantize_price(Decimal("1.000000015")) == Decimal("1.00000002")


def test_quantize_cash_4dp() -> None:
    assert quantize_cash(Decimal("0.02946")) == Decimal("0.0295")


def test_trade_cash_amount_signs() -> None:
    assert trade_cash_amount(Side.BUY, Decimal("0.4"), Decimal("100.02")) == Decimal("-40.0080")
    assert trade_cash_amount(Side.SELL, Decimal(1), Decimal(101)) == Decimal("101.0000")
