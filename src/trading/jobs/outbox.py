"""Transactional outbox: events are written in the same transaction as the state change."""

import json
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.hashing import canonical_json
from trading.monitoring.logging import get_logger

OutboxHandler = Callable[[str, Mapping[str, Any]], None]
_log = get_logger(__name__)


def emit_event(
    session: Session, *, event_type: str, payload: Mapping[str, Any], now: datetime
) -> UUID:
    row = session.execute(
        text(
            "INSERT INTO outbox (event_type, payload_json, created_at)"
            " VALUES (:t, CAST(:p AS jsonb), :now) RETURNING id"
        ),
        {"t": event_type, "p": canonical_json(payload), "now": now},
    ).scalar_one()
    return UUID(str(row))


def log_event(event_type: str, payload: Mapping[str, Any]) -> None:
    """Default sink for event types with no registered handler."""
    _log.warning("outbox_event", event_type=event_type, payload=dict(payload))


def dispatch_pending(
    session: Session, handlers: Mapping[str, OutboxHandler], *, now: datetime, limit: int = 100
) -> int:
    """Deliver undelivered events. A failing handler leaves its event for the next pass."""
    rows = session.execute(
        text(
            "SELECT id, event_type, payload_json FROM outbox WHERE delivered_at IS NULL"
            " ORDER BY created_at, id LIMIT :limit FOR UPDATE SKIP LOCKED"
        ),
        {"limit": limit},
    ).all()
    delivered = 0
    for row in rows:
        payload = (
            row.payload_json if isinstance(row.payload_json, dict) else json.loads(row.payload_json)
        )
        handler = handlers.get(row.event_type, log_event)
        try:
            handler(row.event_type, payload)
        except Exception:
            _log.exception("outbox_handler_failed", event_type=row.event_type, event_id=str(row.id))
            continue
        session.execute(
            text("UPDATE outbox SET delivered_at = :now WHERE id = :id"),
            {"now": now, "id": row.id},
        )
        delivered += 1
    return delivered
