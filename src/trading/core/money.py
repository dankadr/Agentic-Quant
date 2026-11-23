"""Decimal helpers. Binary floats are never accepted for money or quantity."""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Annotated, Any

from pydantic import BeforeValidator

from trading.core.enums import Side
from trading.core.errors import TradingError

_PRICE_Q = Decimal("0.00000001")
_CASH_Q = Decimal("0.0001")


def to_decimal(value: str | int | Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TypeError(f"expected str|int|Decimal, got {type(value).__name__}")
    return Decimal(value)


def quantize_price(v: Decimal) -> Decimal:
    return v.quantize(_PRICE_Q, rounding=ROUND_HALF_EVEN)


def quantize_cash(v: Decimal, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    return v.quantize(_CASH_Q, rounding=rounding)


def floor_to_increment(v: Decimal, inc: Decimal) -> Decimal:
    return (v / inc).to_integral_value(rounding=ROUND_FLOOR) * inc


def ceil_to_increment(v: Decimal, inc: Decimal) -> Decimal:
    return (v / inc).to_integral_value(rounding=ROUND_CEILING) * inc


def is_multiple_of(v: Decimal, inc: Decimal) -> bool:
    return v % inc == 0


def split_position(
    qty: Decimal,
    basis: Decimal,
    ratio_from: Decimal,
    ratio_to: Decimal,
    increment: Decimal = _PRICE_Q,
) -> tuple[Decimal, Decimal]:
    """Reject splits that require residual shares, imprecise basis or an inexact replay factor."""
    if min(ratio_from, ratio_to, increment) <= 0:
        raise TradingError("invalid paper split terms")
    factor = Fraction(ratio_to) / Fraction(ratio_from)
    shares, price = Fraction(qty) * factor, Fraction(basis) / factor
    for value in (factor, shares, price):
        if (value / Fraction(_PRICE_Q)).denominator != 1 or abs(value) >= 10**12:
            raise TradingError("paper split exceeds stored precision")
    if (shares / Fraction(increment)).denominator != 1:
        raise TradingError("paper split produces a nontradable quantity")
    return Decimal(shares.numerator) / shares.denominator, Decimal(
        price.numerator
    ) / price.denominator


def trade_cash_amount(side: Side, qty: Decimal, price: Decimal) -> Decimal:
    """Signed cash effect of a trade: BUY is negative, SELL is positive."""
    gross = quantize_cash(qty * price)
    return -gross if side is Side.BUY else gross


def _reject_float(v: Any) -> Any:
    if isinstance(v, float):
        raise ValueError("floats are not allowed for money/quantities; use a string or Decimal")
    return v


NoFloatDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]
"""Pydantic Decimal that refuses binary floats (YAML/JSON numbers must be quoted strings)."""
