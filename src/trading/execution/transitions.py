"""The one way an order's status changes after creation: validated, versioned, evented."""

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from trading.db.models.trading import Order, OrderEvent
from trading.execution.state_machine import OrderStatus, assert_transition
from trading.jobs.outbox import emit_event
from trading.monitoring.metrics import ORDERS_BY_STATUS


def transition_order(
    session: Session,
    order: Order,
    target: OrderStatus,
    *,
    reason: str | None,
    now: datetime,
    broker_order_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Move ``order`` to ``target``. Raises ``IllegalTransition`` for anything the table forbids."""
    current = OrderStatus(order.status)
    assert_transition(current, target)
    order.status = target.value
    order.status_reason = reason
    if broker_order_id is not None:
        order.broker_order_id = broker_order_id
    order.version += 1
    order.updated_at = now
    session.add(
        OrderEvent(
            order_id=order.id,
            ts=now,
            from_status=current.value,
            to_status=target.value,
            reason=reason,
            payload_json=payload or {},
        )
    )
    session.flush()
    ORDERS_BY_STATUS.labels(target.value).inc()
    emit_event(
        session,
        event_type="order_status_changed",
        payload={
            "client_order_id": order.client_order_id,
            "from": current.value,
            "to": target.value,
            "reason": reason,
        },
        now=now,
    )
