"""Canonical order state machine. Every status change goes through ``assert_transition``."""

from enum import StrEnum

from trading.core.errors import TradingError


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTING = "SUBMITTING"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class IllegalTransition(TradingError):
    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"illegal order transition {current} -> {target}")
        self.current = current
        self.target = target


S = OrderStatus
TERMINAL = frozenset({S.FILLED, S.REJECTED, S.CANCELED, S.EXPIRED})
OPEN_AT_BROKER = frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED})

ALLOWED: dict[OrderStatus, frozenset[OrderStatus]] = {
    S.CREATED: frozenset({S.RISK_APPROVED, S.REJECTED}),
    S.RISK_APPROVED: frozenset({S.SUBMITTING, S.REJECTED, S.CANCELED}),
    S.SUBMITTING: frozenset({S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.REJECTED, S.UNKNOWN}),
    S.ACKNOWLEDGED: frozenset(
        {S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED, S.UNKNOWN}
    ),
    S.PARTIALLY_FILLED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.UNKNOWN}),
    S.UNKNOWN: frozenset(
        {S.ACKNOWLEDGED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.EXPIRED, S.REJECTED}
    ),
    S.FILLED: frozenset(),
    S.REJECTED: frozenset(),
    S.CANCELED: frozenset(),
    S.EXPIRED: frozenset(),
}


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in ALLOWED[current]


def assert_transition(current: OrderStatus, target: OrderStatus) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target)
