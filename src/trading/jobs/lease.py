"""Leases with fencing tokens: single active executor and single scheduler leader.

Every takeover (new holder, or the same holder after expiry) bumps ``fencing_token``.
Writers call ``assert_fence`` inside their transaction, so a stale process whose lease
was taken over fails with ``LeaseLost`` instead of writing.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.errors import TradingError

EXECUTOR_LEASE = "executor"
SCHEDULER_LEASE = "scheduler"


class LeaseLost(TradingError):
    """The caller no longer holds the lease it believes it holds."""


@dataclass(frozen=True)
class Lease:
    name: str
    holder: str
    fencing_token: int
    expires_at: datetime


def _lease(row: object) -> Lease:
    r = row  # sqlalchemy Row with attribute access
    return Lease(
        name=r.name,  # type: ignore[attr-defined]
        holder=r.holder,  # type: ignore[attr-defined]
        fencing_token=r.fencing_token,  # type: ignore[attr-defined]
        expires_at=r.expires_at,  # type: ignore[attr-defined]
    )


def acquire_lease(
    session: Session, *, name: str, holder: str, now: datetime, ttl: timedelta
) -> Lease | None:
    """Take the lease if it is free, expired, or already ours. None if someone else holds it."""
    row = session.execute(
        text(
            "INSERT INTO leases (name, holder, fencing_token, acquired_at, expires_at)"
            " VALUES (:name, :holder, 1, :now, :exp)"
            " ON CONFLICT (name) DO UPDATE SET"
            "  fencing_token = CASE WHEN leases.holder = EXCLUDED.holder"
            "      AND leases.expires_at > :now THEN leases.fencing_token"
            "      ELSE leases.fencing_token + 1 END,"
            "  acquired_at = CASE WHEN leases.holder = EXCLUDED.holder"
            "      AND leases.expires_at > :now THEN leases.acquired_at ELSE :now END,"
            "  holder = EXCLUDED.holder,"
            "  expires_at = EXCLUDED.expires_at"
            " WHERE leases.expires_at <= :now OR leases.holder = EXCLUDED.holder"
            " RETURNING name, holder, fencing_token, expires_at"
        ),
        {"name": name, "holder": holder, "now": now, "exp": now + ttl},
    ).one_or_none()
    return None if row is None else _lease(row)


def renew_lease(session: Session, lease: Lease, *, now: datetime, ttl: timedelta) -> Lease | None:
    """Extend our lease. None if it was taken over or has already expired."""
    row = session.execute(
        text(
            "UPDATE leases SET expires_at = :exp"
            " WHERE name = :name AND holder = :holder AND fencing_token = :tok"
            " AND expires_at > :now RETURNING name, holder, fencing_token, expires_at"
        ),
        {
            "name": lease.name,
            "holder": lease.holder,
            "tok": lease.fencing_token,
            "now": now,
            "exp": now + ttl,
        },
    ).one_or_none()
    return None if row is None else _lease(row)


def release_lease(session: Session, lease: Lease) -> None:
    """Give up our lease immediately. A no-op if it is no longer ours."""
    session.execute(
        text(
            "UPDATE leases SET expires_at = acquired_at"
            " WHERE name = :name AND holder = :holder AND fencing_token = :tok"
        ),
        {"name": lease.name, "holder": lease.holder, "tok": lease.fencing_token},
    )


def current_lease(session: Session, name: str, *, now: datetime) -> Lease | None:
    """The unexpired lease for ``name``, if any."""
    row = session.execute(
        text(
            "SELECT name, holder, fencing_token, expires_at FROM leases"
            " WHERE name = :name AND expires_at > :now"
        ),
        {"name": name, "now": now},
    ).one_or_none()
    return None if row is None else _lease(row)


def assert_fence(session: Session, lease: Lease, *, now: datetime) -> None:
    """Raise ``LeaseLost`` unless ``lease`` is still the valid one.

    Takes a ``FOR SHARE`` row lock, so no takeover can commit between this check and the
    end of the caller's transaction.
    """
    row = session.execute(
        text("SELECT holder, fencing_token, expires_at FROM leases WHERE name = :name FOR SHARE"),
        {"name": lease.name},
    ).one_or_none()
    if (
        row is None
        or row.holder != lease.holder
        or row.fencing_token != lease.fencing_token
        or row.expires_at <= now
    ):
        raise LeaseLost(f"lease {lease.name!r} no longer held by {lease.holder!r}")
