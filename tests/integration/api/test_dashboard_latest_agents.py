from datetime import UTC, datetime, timedelta

from tests.integration.api.conftest import bearer
from trading.agents.runs import record_skip


def test_latest_per_agent_keeps_daily_agents_visible_amid_macro_runs(
    client, viewer_key, session_factory
):
    start = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
    for minute in range(25):
        record_skip(
            session_factory,
            agent_name="macro",
            model="m",
            prompt_version="v",
            reason="no_llm_key",
            now=start + timedelta(minutes=minute),
        )
    record_skip(
        session_factory,
        agent_name="evaluator",
        model="m",
        prompt_version="v",
        reason="no_llm_key",
        now=start,
    )
    items = client.get("/v1/agents/runs?latest_per_agent=true", headers=bearer(viewer_key)).json()[
        "items"
    ]
    assert [r["agent_name"] for r in items] == ["macro", "evaluator"]
    assert items[0]["started_at"].startswith("2026-10-19T14:24")
    assert (
        len(client.get("/v1/agents/runs?limit=20", headers=bearer(viewer_key)).json()["items"])
        == 20
    )


def test_agent_latest_mode_can_scope_the_selected_session(client, viewer_key, session_factory):
    for day in (19, 20):
        record_skip(
            session_factory,
            agent_name="macro",
            model="m",
            prompt_version="v",
            reason="no_llm_key",
            now=datetime(2026, 10, day, 14, 0, tzinfo=UTC),
        )
    response = client.get(
        "/v1/agents/runs?latest_per_agent=true&date=2026-10-19", headers=bearer(viewer_key)
    )
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["started_at"].startswith("2026-10-19")
