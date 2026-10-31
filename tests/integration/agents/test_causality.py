"""A decision may use only what was AVAILABLE at its time, not what had merely started by then."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.agents.macro import latest_macro_view

T = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)


def _insert_macro(
    db: Session, *, started: datetime, available: datetime | None, multiplier: str = "0.5"
) -> None:
    result = {
        "regime": "TRENDING",
        "risk_multiplier": multiplier,
        "valid_until": (T + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at, completed_at,"
            " available_at, result_json, status)"
            " values (gen_random_uuid(), 'macro', 'm', 'macro-v1', :s, :c, :a,"
            " CAST(:r AS jsonb), 'COMPLETED')"
        ),
        {"s": started, "c": available, "a": available, "r": json.dumps(result)},
    )
    db.commit()


def test_a_response_that_arrived_after_the_decision_is_not_used(db: Session) -> None:
    """Started at T, arrived at T+10 min. A decision at T+5 min must not see it."""
    _insert_macro(db, started=T, available=T + timedelta(minutes=10))
    assert latest_macro_view(db, T + timedelta(minutes=5)) is None
    view = latest_macro_view(db, T + timedelta(minutes=11))
    assert view is not None
    assert view.risk_multiplier == Decimal("0.5")


def test_a_legacy_run_without_an_availability_time_is_never_used(db: Session) -> None:
    """Its availability cannot be verified, so it cannot justify a past decision."""
    _insert_macro(db, started=T, available=None)
    assert latest_macro_view(db, T + timedelta(hours=1)) is None
