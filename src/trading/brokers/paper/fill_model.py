"""Deterministic paper fill model: pure function of order, quote, bar, time and configuration.

Fills happen at the quote (ask for buys, bid for sells) plus slippage, rounded against the trader
to the tick size. Limit orders may not fill. Size is capped by a participation ceiling on the
latest closed bar's volume and by displayed size. The bar's high/low/close are never fill prices.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from trading.core.config import PaperBrokerConfig
from trading.core.enums import OrderType, Side
from trading.core.fees import estimate_fees
from trading.core.money import ceil_to_increment, floor_to_increment, quantize_cash
from trading.data.instruments import InstrumentInfo, qty_increment
from trading.data.models import Bar, Quote

_BPS = Decimal(10000)
__all__ = ["FillDecision", "VenueOrderView", "estimate_fees", "evaluate_fill"]


@dataclass(frozen=True)
class VenueOrderView:
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    limit_price: Decimal | None
    qty: Decimal
    filled_qty: Decimal
    eligible_at: datetime
    fill_seq: int


@dataclass(frozen=True)
class FillDecision:
    execution_id: str
    qty: Decimal
    price: Decimal
    fees: Decimal
    slippage_estimate: Decimal


def _usable(quote: Quote | None, now: datetime, cfg: PaperBrokerConfig) -> Quote | None:
    if quote is None or quote.bid <= 0 or quote.ask < quote.bid:
        return None
    if now - quote.ts > timedelta(seconds=cfg.quote_max_age_seconds):
        return None
    return quote


def evaluate_fill(
    order: VenueOrderView,
    *,
    quote: Quote | None,
    bar: Bar | None,
    now: datetime,
    instrument: InstrumentInfo,
    cfg: PaperBrokerConfig,
) -> FillDecision | None:
    """Return the fill for this tick, or None when the order cannot fill now."""
    if now < order.eligible_at:
        return None
    usable = _usable(quote, now, cfg)
    if usable is None:
        return None
    slip = cfg.slippage_bps / _BPS
    tick = instrument.tick_size
    if order.side is Side.BUY:
        price = ceil_to_increment(usable.ask * (1 + slip), tick)
        displayed = usable.ask_size
        if order.order_type is OrderType.LIMIT and (
            order.limit_price is None or price > order.limit_price
        ):
            return None
    else:
        price = floor_to_increment(usable.bid * (1 - slip), tick)
        displayed = usable.bid_size
        if order.order_type is OrderType.LIMIT and (
            order.limit_price is None or price < order.limit_price
        ):
            return None
    if bar is None or bar.volume <= 0:
        return None
    liquidity = bar.volume * cfg.max_participation_rate
    if displayed is not None:
        liquidity = min(liquidity, displayed)
    qty = floor_to_increment(
        min(order.qty - order.filled_qty, liquidity), qty_increment(instrument, cfg)
    )
    if qty <= 0:
        return None
    return FillDecision(
        execution_id=f"{order.client_order_id}-{order.fill_seq + 1}",
        qty=qty,
        price=price,
        fees=estimate_fees(order.side, qty, price, cfg),
        slippage_estimate=quantize_cash(abs(price - usable.mid) * qty),
    )
