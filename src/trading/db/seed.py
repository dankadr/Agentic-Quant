"""Idempotent seed data required for the system to start (always fail-closed)."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session


def seed_defaults(session: Session, now: datetime) -> None:
    """Insert the system_state singleton: PAPER_ONLY, PAUSED, risk state UNKNOWN."""
    session.execute(
        text(
            "INSERT INTO system_state(id, mode, trading_state, risk_state, version, updated_at)"
            " VALUES (1, 'PAPER_ONLY', 'PAUSED', 'UNKNOWN', 1, :now)"
            " ON CONFLICT (id) DO NOTHING"
        ),
        {"now": now},
    )
