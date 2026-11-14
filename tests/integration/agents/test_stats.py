import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import (
    T0,
    make_account,
    make_instrument,
    make_intent,
    make_order,
    make_risk_decision,
    make_snapshot,
)
from trading.agents.stats import agent_decisions, agent_summary

SINCE = T0 - timedelta(hours=24)


def run(
    db: Session, name: str, status: str, minutes: int, result: dict[str, Any] | None = None
) -> None:
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at,"
            " completed_at, result_json, status) values (gen_random_uuid(), :n, 'm', 'v1', :t,"
            " :t, CAST(:r AS jsonb), :s)"
        ),
        {
            "n": name,
            "t": T0 + timedelta(minutes=minutes),
            "r": json.dumps(result or {}),
            "s": status,
        },
    )
    db.flush()


def test_empty_agent_is_idle(db: Session) -> None:
    summary = agent_summary(db, "macro", since=SINCE)
    assert (summary.runs, summary.health, summary.last_run_at) == (0, "idle", None)
    assert agent_decisions(db, "macro") == []


def test_rejected_run_surfaces_error_and_failing_health(db: Session) -> None:
    run(
        db, "evaluator", "REJECTED_INVALID", 5, {"error": "reply does not match", "input_hash": "x"}
    )
    summary = agent_summary(db, "evaluator", since=SINCE)
    assert (summary.health, summary.rejected, summary.last_error) == (
        "failing", 1, "reply does not match"
    )  # fmt: skip
    (decision,) = agent_decisions(db, "evaluator")
    assert (decision.status, decision.error, decision.input_hash) == (
        "REJECTED_INVALID", "reply does not match", "x"
    )  # fmt: skip


def test_tokens_summed_from_result_json(db: Session) -> None:
    run(db, "macro", "COMPLETED", 1, {"tokens": {"prompt": 100, "completion": 40}})
    run(db, "macro", "COMPLETED", 2, {"tokens": {"prompt": 50, "completion": 10}})
    run(db, "macro", "COMPLETED", 3, {})  # no token info: counts as a run, adds nothing
    summary = agent_summary(db, "macro", since=SINCE)
    assert (summary.runs, summary.completed, summary.prompt_tokens, summary.completion_tokens) == (
        3, 3, 150, 50
    )  # fmt: skip
    assert summary.health == "ok"


def test_earlier_failure_makes_health_degraded(db: Session) -> None:
    run(db, "macro", "FAILED", 1, {"error": "timeout"})
    run(db, "macro", "COMPLETED", 2, {})
    assert agent_summary(db, "macro", since=SINCE).health == "degraded"


def test_macro_decision_lists_entry_orders_inside_validity_window(db: Session) -> None:
    started = T0 + timedelta(minutes=10)
    result = {
        "regime": "RANGING",
        "confidence": 0.55,
        "risk_multiplier": "0.7",
        "conclusion": "RANGING: new-entry size x0.7",
        "evidence": ["breadth weak"],
        "limitations": ["46 minutes of data"],
        "input_hash": "abc",
        "valid_until": (started + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    run(db, "macro", "COMPLETED", 10, result)
    account, instrument = make_account(db), make_instrument(db)
    snapshot = make_snapshot(db)
    for i, (minutes, strategy) in enumerate(
        [(20, "orb_v1"), (90, "orb_v1"), (30, "other_v1")], start=1
    ):
        decision = make_risk_decision(
            db,
            account,
            instrument,
            snapshot,
            dedupe_key=f"k{i}",
            now=T0 + timedelta(minutes=minutes),
        )
        intent = make_intent(
            db, account, instrument, decision, client_order_id=f"paper-x-{i}",
            strategy_ids=[strategy], now=T0 + timedelta(minutes=minutes),
        )  # fmt: skip
        make_order(db, intent, now=T0 + timedelta(minutes=minutes))
    (decision,) = agent_decisions(db, "macro")
    assert decision.headline == "RANGING: new-entry size x0.7"
    assert (decision.evidence, decision.limitations) == (("breadth weak",), ("46 minutes of data",))
    assert str(decision.confidence) == "0.55"
    assert decision.affected_orders == ("paper-x-1",)  # inside the window, dependent strategy


def test_non_macro_decisions_have_no_affected_orders(db: Session) -> None:
    run(db, "fundamental", "COMPLETED", 1, {"catalysts": []})
    (decision,) = agent_decisions(db, "fundamental")
    assert decision.affected_orders == ()


def test_summary_reports_last_success_and_skip_beyond_daily_window(db: Session, calendar) -> None:
    from trading.core.config import AgentsConfig

    now = T0 + timedelta(days=5)  # Saturday, market closed
    run(db, "macro", "COMPLETED", 20, {"data_at": (T0 + timedelta(minutes=19)).isoformat()})
    summary = agent_summary(
        db,
        "macro",
        since=now - timedelta(hours=24),
        now=now,
        calendar=calendar,
        agents=AgentsConfig(macro={"enabled": True}),
    )
    assert summary.runs == 0
    assert summary.last_success_at == T0 + timedelta(minutes=20)
    assert summary.health == "idle"
    assert summary.schedule_state == "outside_schedule"
    assert summary.next_scheduled_run > now


def test_skip_reason_and_data_age_visible(db: Session) -> None:
    run(db, "technical", "SKIPPED", 0, {"skip_reason": "no_experiments", "data_at": T0.isoformat()})
    summary = agent_summary(db, "technical", since=SINCE, now=T0 + timedelta(seconds=90))
    assert summary.skipped == 1
    assert summary.last_skip_reason == "no_experiments"
    assert summary.data_age_seconds == 90
    assert summary.health == "idle"


def test_malformed_observation_timestamp_does_not_break_summary(db: Session) -> None:
    run(db, "macro", "COMPLETED", 0, {"data_at": "2026-10-19T13:30:00"})
    summary = agent_summary(db, "macro", since=SINCE, now=T0)
    assert summary.data_age_seconds is None


def test_missing_five_minute_agent_becomes_overdue(db, calendar):
    from trading.core.config import AgentsConfig

    agents = AgentsConfig.model_validate({"data_quality": {"enabled": True}})
    now = datetime(2026, 10, 19, 14, 12, 30, tzinfo=UTC)
    summary = agent_summary(
        db,
        "data_quality",
        since=now - timedelta(hours=24),
        now=now,
        calendar=calendar,
        agents=agents,
    )
    assert summary.health == "degraded"
    assert summary.schedule_state == "overdue"
