"""Shared response types. Money and quantities are serialized as decimal STRINGS, never floats."""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, PlainSerializer, computed_field

from trading.core.enums import PositionEffect

Direction = Literal["LONG", "SHORT"]


def action_label(effect: PositionEffect) -> str:
    return {
        PositionEffect.OPEN_LONG: "BUY to open",
        PositionEffect.CLOSE_LONG: "SELL to close",
        PositionEffect.OPEN_SHORT: "SELL to open",
        PositionEffect.CLOSE_SHORT: "BUY to cover",
    }[effect]


class TradeActionOut(BaseModel):
    side: str
    position_effect: PositionEffect

    @computed_field  # type: ignore[prop-decorator]
    @property
    def action(self) -> str:
        return action_label(self.position_effect)


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != 0 else "0"


def _fixed4(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


Qty = Annotated[Decimal, PlainSerializer(_plain, return_type=str, when_used="json")]
"""Quantities and prices: normalized, e.g. ``0.5`` / ``100.02``."""

Money = Annotated[Decimal, PlainSerializer(_fixed4, return_type=str, when_used="json")]
"""Currency amounts: always four decimals, e.g. ``949.9900``."""
