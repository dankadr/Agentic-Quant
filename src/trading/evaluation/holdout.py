"""Out-of-sample periods are finite: each may be inspected once, and overlaps are visible.

Consumption is transactional. A transaction-scoped advisory lock serializes concurrent requests, so
two experiments cannot both claim the same period, and an overlap with any earlier inspection is
refused rather than silently re-tested.
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.errors import TradingError

_LOCK_KEY = 0x686F6C64  # "hold": one lock for the whole ledger


class HoldoutOverlap(TradingError):
    """The requested period overlaps one already inspected. ``overlaps`` lists them."""

    def __init__(self, overlaps: list[tuple[date, date]]) -> None:
        super().__init__(f"overlaps {len(overlaps)} previously inspected holdout period(s)")
        self.overlaps = overlaps


@dataclass(frozen=True)
class Consumption:
    id: UUID
    start: date
    end: date
    purpose: str


def overlapping(session: Session, start: date, end: date) -> list[tuple[date, date]]:
    rows = session.execute(
        text(
            "SELECT period_start, period_end FROM holdout_consumptions"
            " WHERE period_start <= :end AND period_end >= :start ORDER BY period_start"
        ),
        {"start": start, "end": end},
    ).all()
    return [(r[0], r[1]) for r in rows]


def consume(
    session: Session, *, start: date, end: date, purpose: str, actor: str, now: datetime
) -> Consumption:
    if end < start:
        raise ValueError("holdout end precedes start")
    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    clashes = overlapping(session, start, end)
    if clashes:
        raise HoldoutOverlap(clashes)
    consumption_id = uuid4()
    session.execute(
        text(
            "INSERT INTO holdout_consumptions (id, period_start, period_end, purpose, consumed_by,"
            " consumed_at) VALUES (:id, :s, :e, :p, :a, :t)"
        ),
        {"id": consumption_id, "s": start, "e": end, "p": purpose, "a": actor, "t": now},
    )
    return Consumption(consumption_id, start, end, purpose)
