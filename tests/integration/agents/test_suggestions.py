import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.agents.suggestions import Suggestion, pnl_by_view_class, suggest
from trading.core.config import AgentsConfig

NOW = T0 + timedelta(days=40)
CONFIG = AgentsConfig()


def run(db: Session, name: str, status: str, at: Any, result: dict[str, Any] | None = None) -> None:
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at,"
            " completed_at, result_json, status) values (gen_random_uuid(), :n, 'm', 'v1', :t,"
            " :t, CAST(:r AS jsonb), :s)"
        ),
        {"n": name, "t": at, "r": json.dumps(result or {}), "s": status},
    )
    db.flush()


def macro_runs(db: Session, regimes: list[str], *, step: timedelta) -> None:
    for i, regime in enumerate(regimes):
        run(db, "macro", "COMPLETED", T0 + i * step, {"regime": regime, "risk_multiplier": "0.6"})


def _find(items: list[Suggestion], field: str | None) -> Suggestion:
    (match,) = [s for s in items if s.field == field]
    return match


def test_no_data_returns_empty(db: Session) -> None:
    assert suggest(db, "macro", config=CONFIG, now=NOW) == []
    assert suggest(db, "evaluator", config=CONFIG, now=NOW) == []
    assert suggest(db, "consensus", config=CONFIG, now=NOW) == []


def test_flipping_regimes_below_threshold_is_only_watching(db: Session) -> None:
    macro_runs(db, ["RANGING", "TRENDING"] * 6, step=timedelta(minutes=30))  # 12 runs, 1 day
    s = _find(suggest(db, "macro", config=CONFIG, now=NOW), "run_every_minutes")
    assert (s.status, s.current, s.proposed) == ("watching", 30, 45)
    assert (s.evidence_n, s.evidence_needed) == (12, 30)


def test_flipping_regimes_with_enough_data_is_proposable(db: Session) -> None:
    macro_runs(db, ["RANGING", "TRENDING"] * 15, step=timedelta(days=1))  # 30 runs, 30 days
    s = _find(suggest(db, "macro", config=CONFIG, now=NOW), "run_every_minutes")
    assert (s.status, s.proposed, s.evidence_n) == ("proposable", 45, 30)


def test_stable_regimes_give_no_interval_suggestion(db: Session) -> None:
    macro_runs(db, ["RANGING"] * 30, step=timedelta(days=1))
    assert suggest(db, "macro", config=CONFIG, now=NOW) == []


def test_unknown_share_is_informational_and_never_proposable(db: Session) -> None:
    macro_runs(db, ["UNKNOWN", "RANGING", "UNKNOWN", "RANGING", "RANGING"], step=timedelta(days=1))
    s = _find(suggest(db, "macro", config=CONFIG, now=NOW), None)
    assert (s.status, s.proposed) == ("watching", None)
    assert "UNKNOWN" in s.title


def test_evaluator_failures_surface_without_a_config_change(db: Session) -> None:
    run(db, "evaluator", "REJECTED_INVALID", T0, {"error": "suggestions (too_long)"})
    (s,) = suggest(db, "evaluator", config=CONFIG, now=NOW)
    assert (s.field, s.proposed, s.status) == (None, None, "watching")
    assert "too_long" in s.rationale


def test_pnl_is_grouped_by_the_view_active_when_it_was_booked() -> None:
    views = [(T0, "UNKNOWN"), (T0 + timedelta(hours=1), "RANGING")]
    fills = [
        (T0 + timedelta(minutes=10), Decimal("-0.50")),
        (T0 + timedelta(minutes=50), Decimal("-0.25")),
        (T0 + timedelta(hours=2), Decimal("1.00")),
        (T0 - timedelta(minutes=5), Decimal("9.00")),  # before any view: ignored
    ]
    assert pnl_by_view_class(views, fills) == {
        "UNKNOWN": (2, Decimal("-0.75")),
        "OTHER": (1, Decimal("1.00")),
    }
