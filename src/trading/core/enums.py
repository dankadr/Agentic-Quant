"""Shared enumerations."""

from enum import StrEnum


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    DAY = "DAY"


class AssetType(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"


class PositionEffect(StrEnum):
    OPEN_LONG = "OPEN_LONG"
    CLOSE_LONG = "CLOSE_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    CLOSE_SHORT = "CLOSE_SHORT"

    @property
    def side(self) -> Side:
        return Side.BUY if self in (self.OPEN_LONG, self.CLOSE_SHORT) else Side.SELL

    @property
    def is_entry(self) -> bool:
        return self in (self.OPEN_LONG, self.OPEN_SHORT)

    @property
    def is_short(self) -> bool:
        return self in (self.OPEN_SHORT, self.CLOSE_SHORT)


def effective_effect(side: Side, effect: PositionEffect | None) -> PositionEffect:
    if effect is None:
        return PositionEffect.OPEN_LONG if side is Side.BUY else PositionEffect.CLOSE_LONG
    effect = PositionEffect(effect)
    if effect.side is not side:
        raise ValueError(f"{effect} requires {effect.side}, received {side}")
    return effect
