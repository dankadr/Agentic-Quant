"""Capital accounting for a strategy allocation inside a larger broker account.

The strategy has a fixed initial allocation (for example $1,000). Its equity is that allocation
plus its own net profit or loss. Risk decisions use the LOWER of strategy equity and broker equity,
so a larger paper account never enlarges the strategy's budget. Margin buying power is never used.
"""

from decimal import Decimal

ZERO = Decimal(0)


def strategy_equity(allocation: Decimal, net_pnl: Decimal) -> Decimal:
    return allocation + net_pnl


def risk_equity(strategy: Decimal, broker: Decimal) -> Decimal:
    """The base every risk limit is computed from: never more than the strategy owns."""
    return min(strategy, broker)


def deployable_cash(
    strategy_cash: Decimal, broker_cash: Decimal, open_buy_reserve: Decimal
) -> Decimal:
    """Cash the strategy may commit now. Unallocated broker cash is excluded by the minimum, and
    cash already reserved by open buy orders is not available twice."""
    available = min(strategy_cash, broker_cash) - open_buy_reserve
    return max(ZERO, available)
