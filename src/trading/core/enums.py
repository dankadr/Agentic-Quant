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
