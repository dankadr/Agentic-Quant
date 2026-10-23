"""Fee estimation shared by the paper broker, the risk engine and cash reservations."""

from decimal import ROUND_UP, Decimal

from trading.core.config import PaperBrokerConfig
from trading.core.enums import Side
from trading.core.money import quantize_cash


def estimate_fees(side: Side, qty: Decimal, price: Decimal, cfg: PaperBrokerConfig) -> Decimal:
    """Commission on every order; regulatory fees on sells only. Rounded up (conservative)."""
    fees = cfg.commission_per_order_usd
    if side is Side.SELL:
        fees += qty * price * cfg.sell_fee_rate
        fees += min(qty * cfg.taf_per_share_usd, cfg.taf_max_per_trade_usd)
    return quantize_cash(fees, ROUND_UP)
