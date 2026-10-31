"""Incidents: one open record per condition, a delivery queued on open and on recovery.

Detection runs again and again. Repeats while an incident is open change nothing and send nothing,
so a stuck condition produces one actionable alert, and clearing it produces one recovery notice.
Text is scrubbed of secret-looking values and capped before it is stored.
"""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.db.models.ops_alerts import IncidentDeliveryRow, IncidentRow
from trading.monitoring.logging import scrub_text

MAX_TEXT = 500
CHANNEL = "telegram"
CRITICAL = "CRITICAL"
WARNING = "WARNING"


def _clean(text: str) -> str:
    return scrub_text(text)[:MAX_TEXT]


def open_incident(session: Session, key: str) -> IncidentRow | None:
    return session.scalar(
        select(IncidentRow).where(IncidentRow.incident_key == key, IncidentRow.state == "OPEN")
    )


def raise_incident(
    session: Session, *, key: str, severity: str, title: str, detail: str, now: datetime
) -> bool:
    """Open the incident and queue its notice. Returns False if it was already open (no notice)."""
    if severity not in {CRITICAL, WARNING}:
        raise ValueError(f"unknown severity {severity!r}")
    if open_incident(session, key) is not None:
        return False
    incident = IncidentRow(
        id=uuid4(),
        incident_key=key,
        severity=severity,
        state="OPEN",
        title=_clean(title),
        detail=_clean(detail),
        opened_at=now,
    )
    session.add(incident)
    session.flush()
    _queue(session, incident.id, "OPENED", now)
    return True


def resolve_incident(session: Session, *, key: str, now: datetime) -> bool:
    """Close the incident and queue one recovery notice. False if nothing was open."""
    incident = open_incident(session, key)
    if incident is None:
        return False
    incident.state = "RESOLVED"
    incident.resolved_at = now
    session.flush()
    _queue(session, incident.id, "RECOVERED", now)
    return True


def _queue(session: Session, incident_id: UUID, event: str, now: datetime) -> None:
    session.add(
        IncidentDeliveryRow(
            id=uuid4(),
            incident_id=incident_id,
            event=event,
            channel=CHANNEL,
            state="PENDING",
            attempts=0,
            next_attempt_at=now,
            created_at=now,
        )
    )


def acknowledge(session: Session, incident_id: UUID, *, by: str, now: datetime) -> bool:
    incident = session.get(IncidentRow, incident_id)
    if incident is None or incident.acknowledged_at is not None:
        return False
    incident.acknowledged_by = by
    incident.acknowledged_at = now
    return True


def retry_after(attempts: int) -> timedelta:
    """Exponential backoff, capped: a notice keeps trying for a while, never forever."""
    return timedelta(seconds=min(2**attempts, 900))
