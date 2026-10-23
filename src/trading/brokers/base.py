"""Broker adapter interface. PaperBroker is the only implementation in the MVP.

Nothing outside the trading worker may construct a ``BrokerAdapter``. No live adapter exists and
none may be added without the separately authorized live phase (spec Sprint 6).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from trading.core.enums import OrderType, Side, TimeInForce
from trading.core.errors import TradingError


class BrokerTimeout(TradingError):
    """The call may or may not have taken effect. Reconcile before retrying."""


class BrokerUnavailable(TradingError):
    """The broker could not be reached."""


class BrokerRejected(TradingError):
    """The broker refused the request; ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BrokerOrderStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class BrokerOrderRequest:
    client_order_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Decimal
    limit_price: Decimal | None
    time_in_force: TimeInForce


@dataclass(frozen=True)
class BrokerOrderState:
    client_order_id: str
    broker_order_id: str
    status: BrokerOrderStatus
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    updated_at: datetime


@dataclass(frozen=True)
class BrokerFill:
    execution_id: str
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: Side
    qty: Decimal
    price: Decimal
    fees: Decimal
    slippage_estimate: Decimal
    ts: datetime


@dataclass(frozen=True)
class BrokerAccountState:
    cash: Decimal
    positions: Mapping[str, Decimal]
    as_of: datetime


class BrokerAdapter(Protocol):
    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrderState: ...

    def cancel_order(self, client_order_id: str) -> BrokerOrderState: ...

    def get_order(self, client_order_id: str) -> BrokerOrderState | None: ...

    def list_open_orders(self) -> Sequence[BrokerOrderState]: ...

    def list_fills(self, *, since: datetime | None = None) -> Sequence[BrokerFill]:
        """Fills ordered by ``(ts, execution_id)``."""
        ...

    def get_account(self) -> BrokerAccountState: ...
