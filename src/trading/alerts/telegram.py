"""Deliver queued incident notices to the one configured private Telegram chat.

Runs on the research/operations worker, never on the executor, so a slow or failing Telegram cannot
hold up a tick, an exit, or an operator command. Each notice has a bounded number of attempts. Only
the incident's own title and detail are sent: never a report, a payload, or a credential.
"""

import logging
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.alerts.store import retry_after
from trading.core.errors import TradingError
from trading.db.models.ops_alerts import IncidentDeliveryRow, IncidentRow
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
# httpx logs every request URL at INFO, and this URL carries the bot token. Keep it out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
API_HOST = "api.telegram.org"
MAX_ATTEMPTS = 6
BATCH = 20


class ChannelRefused(TradingError):
    """The channel refused the notice (bad token, chat, or configuration). Not retried blindly."""


def _text(incident: IncidentRow, event: str) -> str:
    if event == "RECOVERED":
        return f"RECOVERED: {incident.title}\n{incident.detail}"
    return f"[{incident.severity}] {incident.title}\n{incident.detail}"


def dispatch_due(
    session_factory: sessionmaker[Session],
    client: httpx.Client,
    *,
    bot_token: str,
    chat_id: str,
    now: datetime,
) -> int:
    """Send every due notice once. Returns how many were delivered."""
    with session_factory() as session:
        due = list(
            session.scalars(
                select(IncidentDeliveryRow)
                .where(
                    IncidentDeliveryRow.state == "PENDING",
                    IncidentDeliveryRow.next_attempt_at <= now,
                )
                .order_by(IncidentDeliveryRow.created_at)
                .limit(BATCH)
            )
        )
        pending = [(d.id, d.incident_id, d.event, d.attempts) for d in due]
    sent = 0
    for delivery_id, incident_id, event, attempts in pending:
        with session_factory() as session:
            incident = session.get(IncidentRow, incident_id)
            assert incident is not None  # noqa: S101 - foreign key guarantees it
            text = _text(incident, event)
        error: str | None = None
        try:
            _send(client, bot_token=bot_token, chat_id=chat_id, text=text)
        except (httpx.HTTPError, ChannelRefused) as exc:
            error = f"{type(exc).__name__}"
            _log.warning("alert_delivery_failed", delivery=str(delivery_id), error=error)
        with session_factory.begin() as session:
            row = session.get(IncidentDeliveryRow, delivery_id)
            assert row is not None  # noqa: S101 - we just read it
            if error is None:
                row.state = "SENT"
                row.sent_at = now
                row.attempts = attempts + 1
                sent += 1
            else:
                row.attempts = attempts + 1
                row.last_error = error
                if row.attempts >= MAX_ATTEMPTS:
                    row.state = "FAILED"  # visible on the dashboard; not retried forever
                else:
                    row.next_attempt_at = now + retry_after(row.attempts)
    return sent


def _send(client: httpx.Client, *, bot_token: str, chat_id: str, text: str) -> None:
    response = client.post(
        f"https://{API_HOST}/bot{bot_token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=10.0,
        follow_redirects=False,
    )
    if response.status_code == 429 or response.status_code >= 500:
        raise httpx.HTTPError(f"HTTP {response.status_code}")
    if response.status_code != 200:
        raise ChannelRefused(f"HTTP {response.status_code}")
