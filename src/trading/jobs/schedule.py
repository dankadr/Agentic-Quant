"""Pure job planning: what should exist in the queue at ``now``. The scheduler enqueues it."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from trading.core.time import ensure_utc


@dataclass(frozen=True)
class JobSpec:
    kind: str
    dedupe_key: str
    scheduled_at: datetime
    payload: Mapping[str, Any]


def plan_jobs(now: datetime, calendar: object | None) -> list[JobSpec]:
    """Jobs due at ``now``. Dedupe keys make repeated planning within a minute harmless.

    Sprint 1 plans only the per-minute heartbeat; session jobs arrive with the calendar (Task 25).
    """
    minute = ensure_utc(now).replace(second=0, microsecond=0)
    return [
        JobSpec(
            kind="heartbeat",
            dedupe_key=f"heartbeat:{minute:%Y-%m-%dT%H:%MZ}",
            scheduled_at=minute,
            payload={},
        )
    ]
