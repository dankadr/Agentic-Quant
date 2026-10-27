"""Operational tables: auth, idempotency, jobs, leases, outbox, audit, system state."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, SmallInteger
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import Base


class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    role: Mapped[str]
    api_key_hash: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime]
    disabled_at: Mapped[datetime | None]


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    principal_id: Mapped[UUID] = mapped_column(ForeignKey("principals.id"), primary_key=True)
    route: Mapped[str] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    request_hash: Mapped[str]
    status_code: Mapped[int]
    response_json: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str]
    payload_json: Mapped[dict[str, Any]]
    state: Mapped[str]
    scheduled_at: Mapped[datetime]
    lease_until: Mapped[datetime | None]
    locked_by: Mapped[str | None]
    attempts: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    last_error: Mapped[str | None]
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class LeaseRow(Base):
    __tablename__ = "leases"
    name: Mapped[str] = mapped_column(primary_key=True)
    holder: Mapped[str]
    fencing_token: Mapped[int]
    acquired_at: Mapped[datetime]
    expires_at: Mapped[datetime]


class OutboxRow(Base):
    __tablename__ = "outbox"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_type: Mapped[str]
    payload_json: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]


class AuditEventRow(Base):
    __tablename__ = "audit_events"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str]
    ts: Mapped[datetime]
    request_id: Mapped[str | None]
    before_hash: Mapped[str | None]
    after_hash: Mapped[str | None]
    result: Mapped[str]
    details_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class OperationalCommandRow(Base):
    """An operator's stop/cancel/flatten request, tracked until the executor finishes it."""

    __tablename__ = "operational_commands"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str]
    status: Mapped[str]
    requested_by: Mapped[str]
    request_id: Mapped[str | None]
    reason: Mapped[str]
    requested_version: Mapped[int]
    requested_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    progress_json: Mapped[dict[str, Any]] = mapped_column(default=dict)


class SystemStateRow(Base):
    __tablename__ = "system_state"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    mode: Mapped[str]
    trading_state: Mapped[str]
    risk_state: Mapped[str]
    risk_reason: Mapped[str | None]
    version: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[datetime]
    last_reconciled_at: Mapped[datetime | None]
