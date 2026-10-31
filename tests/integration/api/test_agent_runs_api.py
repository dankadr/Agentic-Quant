from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.api.conftest import bearer
from trading.agents.runs import record_skip

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)


def test_skipped_runs_say_why_and_unknown_cost_is_null(
    client: TestClient,
    viewer_key: str,
    session_factory: sessionmaker[Session],
) -> None:
    record_skip(
        session_factory,
        agent_name="macro",
        model="qwen-test",
        prompt_version="macro-v1",
        reason="no_llm_key",
        now=NOW,
    )
    response = client.get("/v1/agent-runs", headers=bearer(viewer_key))
    assert response.status_code == 200
    (item,) = response.json()["items"]
    assert item["status"] == "SKIPPED"
    assert item["skip_reason"] == "no_llm_key"
    assert item["cost_usd"] is None  # unknown, never implied zero
    assert item["available_at"] is None  # a skipped run made nothing available


def test_the_agent_filter_and_limit_are_applied(
    client: TestClient,
    viewer_key: str,
    session_factory: sessionmaker[Session],
) -> None:
    record_skip(
        session_factory,
        agent_name="macro",
        model="m",
        prompt_version="v",
        reason="no_llm_key",
        now=NOW,
    )
    record_skip(
        session_factory,
        agent_name="consensus",
        model="m",
        prompt_version="v",
        reason="other",
        now=NOW,
    )
    only_macro = client.get(
        "/v1/agent-runs", headers=bearer(viewer_key), params={"agent_name": "macro"}
    ).json()["items"]
    assert [r["agent_name"] for r in only_macro] == ["macro"]
    limited = client.get("/v1/agent-runs", headers=bearer(viewer_key), params={"limit": 1}).json()[
        "items"
    ]
    assert len(limited) == 1
