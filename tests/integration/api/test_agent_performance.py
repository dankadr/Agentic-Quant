from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.integration.api.conftest import bearer
from trading.agents.registry import AGENT_REGISTRY
from trading.core.clock import ManualClock
from trading.db.models.research import AgentRun


def add_run(
    db: Session,
    clock: ManualClock,
    *,
    name: str = "macro",
    status: str = "COMPLETED",
    version: str | None = None,
    model: str = "model-a",
    delta: int = -60,
    measured: bool = False,
    future_completion: bool = False,
) -> None:
    started = clock.now() + timedelta(seconds=delta)
    result = (
        {"telemetry_version": "run-telemetry-v1", "completion_clock": "runtime"} if measured else {}
    )
    db.add(
        AgentRun(
            agent_name=name,
            model=model,
            prompt_version=version or AGENT_REGISTRY[name].prompt_version,
            started_at=started,
            completed_at=clock.now() + timedelta(seconds=60)
            if future_completion
            else started + timedelta(seconds=10),
            status=status,
            result_json=result,
            prompt_tokens=100 if measured else None,
            completion_tokens=20 if measured else None,
            cost_usd=Decimal(0),
        )
    )
    db.commit()


def test_scorecards_require_auth_and_keep_unknown_agents_unscored(
    client: TestClient, viewer_key: str
) -> None:
    assert client.get("/v1/agents/performance").status_code == 401
    response = client.get("/v1/agents/performance", headers=bearer(viewer_key))
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 8
    for card in body["items"]:
        assert card["current"]["reliability"]["score"] is None
        assert card["current"]["contribution"]["score"] is None
        assert card["current"]["quality"]["factual_accuracy_score"] is None
        assert card["current"]["counts"]["total"] == 0
    assert (
        client.get("/v1/agents/performance?days=0", headers=bearer(viewer_key)).status_code == 422
    )


def test_scorecard_denominators_versions_and_missing_telemetry(
    client: TestClient, db: Session, clock: ManualClock, viewer_key: str
) -> None:
    for status in ["COMPLETED", "FAILED", "REJECTED_INVALID", "SKIPPED", "RUNNING"]:
        add_run(db, clock, status=status, measured=status == "COMPLETED")
    add_run(db, clock, version="legacy-v0")
    add_run(db, clock, model="model-b")
    add_run(db, clock, name="evaluator", version="evaluator-v1")
    response = client.get("/v1/agents/performance", headers=bearer(viewer_key))
    assert response.status_code == 200
    cards = {c["name"]: c for c in response.json()["items"]}
    cohort = next(
        c
        for c in cards["macro"]["cohorts"]
        if c["model"] == "model-a" and c["prompt_version"] == AGENT_REGISTRY["macro"].prompt_version
    )
    assert cohort["counts"] == {
        "total": 5,
        "completed": 1,
        "failed": 1,
        "invalid": 1,
        "skipped": 1,
        "inflight": 1,
    }
    assert round(cohort["reliability"]["score"], 2) == 33.33
    assert cohort["reliability"]["denominator"] == 3
    assert cohort["quality"]["output_acceptance_score"] == 50
    assert cohort["quality"]["factual_accuracy_score"] is None
    assert cohort["observability"]["latency_sample"] == 1
    assert cohort["observability"]["median_latency_seconds"] == 10
    assert cohort["observability"]["known_usage_runs"] == 1
    assert cohort["observability"]["known_cost_runs"] == 0
    assert cohort["observability"]["cost_usd"] is None
    assert cards["evaluator"]["current"]["counts"]["total"] == 0
    assert cards["evaluator"]["legacy_runs"] == 1
    assert len(cards["macro"]["cohorts"]) == 3


def test_future_and_out_of_window_data_cannot_inflate_scores(
    client: TestClient, db: Session, clock: ManualClock, viewer_key: str
) -> None:
    add_run(db, clock, delta=60)
    add_run(db, clock, delta=-60, future_completion=True)
    add_run(db, clock, delta=-31 * 86400)
    body = client.get("/v1/agents/performance?days=30", headers=bearer(viewer_key)).json()
    card = next(c for c in body["items"] if c["name"] == "macro")
    assert len(card["cohorts"]) == 1
    cohort = card["cohorts"][0]
    assert cohort["counts"]["total"] == 1
    assert cohort["counts"]["inflight"] == 1
    assert cohort["reliability"]["score"] is None


def test_legacy_consensus_pnl_is_excluded_and_not_promoted_to_alpha(
    client: TestClient, db: Session, clock: ManualClock, viewer_key: str
) -> None:
    add_run(db, clock, name="consensus", version="consensus-v1", model="deterministic")
    row = db.query(AgentRun).filter_by(agent_name="consensus").one()
    row.result_json = {"value_added": "235.9416", "verdict": "SHADOW_ONLY"}
    db.commit()
    card = next(
        c
        for c in client.get("/v1/agents/performance", headers=bearer(viewer_key)).json()["items"]
        if c["name"] == "consensus"
    )
    assert card["legacy_runs"] == 1
    assert card["current"]["contribution"]["score"] is None
    assert card["current"]["contribution"]["status"] == "not_measured"
    assert "235.9416" not in str(card)


def test_unreported_usage_and_legacy_zero_latency_stay_unknown(
    client: TestClient, db: Session, clock: ManualClock, viewer_key: str
) -> None:
    add_run(db, clock, measured=True)
    row = db.query(AgentRun).one()
    row.result_json = {**row.result_json, "usage_reported": False}
    row.prompt_tokens = row.completion_tokens = 0
    db.commit()
    card = next(
        c
        for c in client.get("/v1/agents/performance", headers=bearer(viewer_key)).json()["items"]
        if c["name"] == "macro"
    )
    cohort = card["cohorts"][0]
    assert cohort["observability"]["known_usage_runs"] == 0
    assert cohort["observability"]["prompt_tokens"] is None
    assert cohort["observability"]["median_latency_seconds"] == 10
