"""Trade-update events and the fill de-duplication that keeps streamed and polled fills apart.

A fill can arrive on the trade-update stream and again in the account activities returned by REST.
Both paths key on the execution id, so a fill is recorded once however many times it is seen.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from trading.brokers.base import BrokerFill
from trading.core.enums import Side

FILL_EVENTS = frozenset({"fill", "partial_fill"})


@dataclass(frozen=True)
class TradeUpdate:
    event: str
    client_order_id: str
    broker_order_id: str
    execution_id: str | None
    symbol: str
    side: Side
    qty: Decimal | None
    price: Decimal | None
    ts: datetime


def parse_trade_update(message: Mapping[str, Any]) -> TradeUpdate | None:
    """One stream message. Non-trade-update messages (auth, listening) return None."""
    if message.get("stream") != "trade_updates":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    order = data.get("order")
    if not isinstance(order, dict):
        return None
    raw_ts = str(data.get("timestamp") or order.get("updated_at"))
    return TradeUpdate(
        event=str(data["event"]),
        client_order_id=str(order["client_order_id"]),
        broker_order_id=str(order["id"]),
        execution_id=str(data["execution_id"]) if data.get("execution_id") else None,
        symbol=str(order["symbol"]),
        side=Side.BUY if str(order["side"]).lower() == "buy" else Side.SELL,
        qty=Decimal(str(data["qty"])) if data.get("qty") else None,
        price=Decimal(str(data["price"])) if data.get("price") else None,
        ts=datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(UTC),
    )


def new_fills(updates: Iterable[TradeUpdate], seen: set[str]) -> list[BrokerFill]:
    """Fills not yet recorded. ``seen`` is updated in place; persist it with the ledger."""
    fresh: list[BrokerFill] = []
    for update in updates:
        if update.event not in FILL_EVENTS or update.execution_id is None:
            continue
        if update.execution_id in seen or update.qty is None or update.price is None:
            continue
        seen.add(update.execution_id)
        fresh.append(
            BrokerFill(
                execution_id=update.execution_id,
                client_order_id=update.client_order_id,
                broker_order_id=update.broker_order_id,
                symbol=update.symbol,
                side=update.side,
                qty=update.qty,
                price=update.price,
                fees=Decimal(0),
                slippage_estimate=Decimal(0),
                ts=update.ts,
            )
        )
    return sorted(fresh, key=lambda f: (f.ts, f.execution_id))
