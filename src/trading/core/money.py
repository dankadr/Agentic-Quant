"""Decimal helpers. Binary floats are never accepted for money or quantity."""

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

from trading.core.enums import Side

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


def trade_cash_amount(side: Side, qty: Decimal, price: Decimal) -> Decimal:
    """Signed cash effect of a trade: BUY is negative, SELL is positive."""
    gross = quantize_cash(qty * price)
    return -gross if side is Side.BUY else gross
