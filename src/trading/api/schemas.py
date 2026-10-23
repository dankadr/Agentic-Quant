"""Shared response types. Money and quantities are serialized as decimal STRINGS, never floats."""

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _fixed4(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


Qty = Annotated[Decimal, PlainSerializer(_plain, return_type=str, when_used="json")]
"""Quantities and prices: normalized, e.g. ``0.5`` / ``100.02``."""

Money = Annotated[Decimal, PlainSerializer(_fixed4, return_type=str, when_used="json")]
"""Currency amounts: always four decimals, e.g. ``949.9900``."""
