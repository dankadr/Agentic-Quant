"""Incidents, their alert deliveries, and dashboard sessions."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import Base


class IncidentRow(Base):
    __tablename__ = "incidents"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_key: Mapped[str]
    severity: Mapped[str]
    state: Mapped[str]
    title: Mapped[str]
    detail: Mapped[str]
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    acknowledged_by: Mapped[str | None]
    acknowledged_at: Mapped[datetime | None]


class IncidentDeliveryRow(Base):
    __tablename__ = "incident_deliveries"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    incident_id: Mapped[UUID] = mapped_column(ForeignKey("incidents.id"))
    event: Mapped[str]
    channel: Mapped[str]
    state: Mapped[str]
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime]
    last_error: Mapped[str | None]
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]


class UiSessionRow(Base):
    __tablename__ = "ui_sessions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(unique=True)
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"))
    csrf_token: Mapped[str]
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
