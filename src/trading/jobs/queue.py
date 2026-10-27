"""Durable job queue on PostgreSQL using ``FOR UPDATE SKIP LOCKED`` claims.

Delivery is at-least-once: a worker that dies mid-job leaves a RUNNING row whose
lease expires, after which another worker reclaims it. Handlers must be idempotent.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session
from sqlalchemy.types import Text

from trading.core.hashing import canonical_json
from trading.monitoring.logging import scrub_text

_MAX_ERROR_CHARS = 2000
_MAX_BACKOFF_SECONDS = 300


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    DEAD = "DEAD"


@dataclass(frozen=True)
class Job:
    id: UUID
    kind: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    session: Session,
    *,
    kind: str,
    payload: Mapping[str, Any],
    scheduled_at: datetime,
    dedupe_key: str,
    max_attempts: int = 5,
) -> UUID:
    """Insert a job, or return the existing job's id when ``dedupe_key`` is already used.

    ``created_at``/``updated_at`` use ``scheduled_at`` so job rows stay deterministic
    under a ``ManualClock``.
    """
    params = {
        "kind": kind,
        "payload": canonical_json(payload),
        "at": scheduled_at,
        "key": dedupe_key,
        "max": max_attempts,
    }
    inserted = session.execute(
        text(
            "INSERT INTO jobs (kind, payload_json, state, scheduled_at, dedupe_key,"
            " max_attempts, created_at, updated_at)"
            " VALUES (:kind, CAST(:payload AS jsonb), 'QUEUED', :at, :key, :max, :at, :at)"
            " ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"
        ),
        params,
    ).scalar_one_or_none()
    if inserted is not None:
        return UUID(str(inserted))
    existing = session.execute(
        text("SELECT id FROM jobs WHERE dedupe_key = :key"), {"key": dedupe_key}
    ).scalar_one()
    return UUID(str(existing))


def claim(
    session: Session,
    *,
    worker_id: str,
    now: datetime,
    lease_for: timedelta,
    kinds: Collection[str] | None = None,
) -> Job | None:
    """Atomically take one due job (or one whose worker lease expired)."""
    # A job whose lease expired after its final attempt is dead, not retried forever.
    session.execute(
        text(
            "UPDATE jobs SET state = 'DEAD', lease_until = NULL, locked_by = NULL,"
            " last_error = 'lease expired after max attempts', updated_at = :now"
            " WHERE state = 'RUNNING' AND lease_until < :now AND attempts >= max_attempts"
        ),
        {"now": now},
    )
    kind_filter = "AND kind = ANY(:kinds)" if kinds is not None else ""
    stmt = text(  # fixed fragment only; all values are bound parameters
        "WITH c AS (SELECT id FROM jobs"  # noqa: S608
        " WHERE ((state = 'QUEUED' AND scheduled_at <= :now)"
        "     OR (state = 'RUNNING' AND lease_until < :now))"
        f" {kind_filter}"
        " ORDER BY scheduled_at, id FOR UPDATE SKIP LOCKED LIMIT 1)"
        " UPDATE jobs j SET state = 'RUNNING', lease_until = :lease_until, locked_by = :worker,"
        " attempts = j.attempts + 1, updated_at = :now FROM c WHERE j.id = c.id"
        " RETURNING j.id, j.kind, j.payload_json, j.attempts, j.max_attempts"
    )
    params: dict[str, Any] = {"now": now, "lease_until": now + lease_for, "worker": worker_id}
    if kinds is not None:
        stmt = stmt.bindparams(bindparam("kinds", type_=ARRAY(Text)))
        params["kinds"] = list(kinds)
    row = session.execute(stmt, params).one_or_none()
    if row is None:
        return None
    payload = (
        row.payload_json if isinstance(row.payload_json, dict) else json.loads(row.payload_json)
    )
    return Job(
        id=UUID(str(row.id)),
        kind=row.kind,
        payload=payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


def complete(
    session: Session, job_id: UUID, *, worker_id: str, attempts: int, now: datetime
) -> bool:
    """Mark the job succeeded, only if this exact claim (worker + attempt) still owns it.

    Returns False when the lease expired and the job was reclaimed: the stale result is dropped.
    """
    row = session.execute(
        text(
            "UPDATE jobs SET state = 'SUCCEEDED', lease_until = NULL, locked_by = NULL,"
            " updated_at = :now WHERE id = :id AND state = 'RUNNING'"
            " AND locked_by = :worker AND attempts = :attempts RETURNING id"
        ),
        {"id": job_id, "now": now, "worker": worker_id, "attempts": attempts},
    ).one_or_none()
    return row is not None


def renew(
    session: Session,
    job_id: UUID,
    *,
    worker_id: str,
    attempts: int,
    now: datetime,
    lease_for: timedelta,
) -> bool:
    """Extend a running job's lease. False when this claim no longer owns it (it expired and
    was reclaimed, or the job finished): the caller must treat its result as obsolete."""
    row = session.execute(
        text(
            "UPDATE jobs SET lease_until = :until, updated_at = :now"
            " WHERE id = :id AND state = 'RUNNING' AND locked_by = :worker AND attempts = :attempts"
            " AND lease_until >= :now RETURNING id"
        ),
        {
            "id": job_id,
            "until": now + lease_for,
            "now": now,
            "worker": worker_id,
            "attempts": attempts,
        },
    ).one_or_none()
    return row is not None


def fail(
    session: Session, job_id: UUID, *, worker_id: str, attempts: int, error: str, now: datetime
) -> JobState | None:
    """Record a failure: requeue with exponential backoff, or DEAD after max attempts.

    Returns None (and changes nothing) when this claim no longer owns the job.
    """
    row = session.execute(
        text(
            "SELECT attempts, max_attempts FROM jobs WHERE id = :id AND state = 'RUNNING'"
            " AND locked_by = :worker AND attempts = :attempts FOR UPDATE"
        ),
        {"id": job_id, "worker": worker_id, "attempts": attempts},
    ).one_or_none()
    if row is None:
        return None
    dead = row.attempts >= row.max_attempts
    delay = timedelta(seconds=min(2**row.attempts, _MAX_BACKOFF_SECONDS))
    session.execute(
        text(
            "UPDATE jobs SET state = :state, scheduled_at = :at, lease_until = NULL,"
            " locked_by = NULL, last_error = :err, updated_at = :now WHERE id = :id"
        ),
        {
            "id": job_id,
            "state": (JobState.DEAD if dead else JobState.QUEUED).value,
            "at": now if dead else now + delay,
            "err": scrub_text(error)[:_MAX_ERROR_CHARS],
            "now": now,
        },
    )
    return JobState.DEAD if dead else JobState.QUEUED
