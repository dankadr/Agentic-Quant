"""Append-only audit trail. Only content hashes of before/after states are stored."""

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from trading.core.hashing import canonical_json, content_hash
from trading.db.models.ops import AuditEventRow
from trading.monitoring.logging import redact


def record_audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target: str,
    request_id: str | None,
    before: object | None,
    after: object | None,
    result: str,
    now: datetime,
    details: Mapping[str, Any] | None = None,
) -> UUID:
    """Insert one audit event in the caller's transaction and return its id.

    ``details`` is passed through the log redactor; it must still never carry secrets.
    """
    safe_details = json.loads(canonical_json(redact(None, "audit", dict(details or {}))))
    row = AuditEventRow(
        actor=actor,
        action=action,
        target=target,
        ts=now,
        request_id=request_id,
        before_hash=content_hash(before),
        after_hash=content_hash(after),
        result=result,
        details_json=safe_details,
    )
    session.add(row)
    session.flush()
    return row.id


def list_audit(
    session: Session, *, limit: int, before: tuple[datetime, UUID] | None
) -> list[AuditEventRow]:
    """Newest-first keyset page; pass the last row's ``(ts, id)`` as ``before`` for the next."""
    stmt = select(AuditEventRow).order_by(AuditEventRow.ts.desc(), AuditEventRow.id.desc())
    if before is not None:
        stmt = stmt.where(tuple_(AuditEventRow.ts, AuditEventRow.id) < tuple_(*before))
    return list(session.scalars(stmt.limit(limit)))
