import json
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from tests.integration.api.conftest import bearer

NEW = {"changes": {"run_every_minutes": 15}, "rationale": "chatty regime flips"}


def agent_run(db: Session, name: str, status: str, minutes: int, result: dict[str, Any]) -> None:
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at,"
            " completed_at, result_json, status) values (gen_random_uuid(), :n, 'm', 'v1', :t,"
            " :t, CAST(:r AS jsonb), :s)"
        ),
        {"n": name, "t": T0 + timedelta(minutes=minutes), "r": json.dumps(result), "s": status},
    )
    db.commit()


def post(client: TestClient, path: str, key: str, body: dict[str, Any], idem: str | None = "k1"):  # type: ignore[no-untyped-def]
    headers = bearer(key)
    if idem:
        headers["Idempotency-Key"] = idem
    return client.post(path, headers=headers, json=body)


def test_list_covers_every_agent_with_health(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    agent_run(db, "evaluator", "REJECTED_INVALID", -5, {"error": "too long"})
    body = client.get("/v1/agents", headers=bearer(viewer_key)).json()
    by_name = {a["name"]: a for a in body["items"]}
    assert set(by_name) == {"macro", "fundamental", "evaluator", "consensus", "technical"}
    assert (by_name["evaluator"]["health"], by_name["macro"]["health"]) == ("failing", "idle")
    assert by_name["macro"]["enabled"] is True  # configs/agents.yaml
    assert by_name["consensus"]["uses_llm"] is False


def test_empty_agent_detail_is_200_and_idle(client: TestClient, viewer_key: str) -> None:
    body = client.get("/v1/agents/macro", headers=bearer(viewer_key)).json()
    assert body["summary"]["health"] == "idle"
    assert body["info"]["name"] == "macro" and body["info"]["prompt_version"] == "macro-v1"
    fields = {f["name"]: f for f in body["config"]}
    assert (fields["run_every_minutes"]["value"], fields["run_every_minutes"]["pending"]) == (
        30,
        None,
    )
    assert "risk" in body["locked"]
    assert (
        client.get("/v1/agents/macro/decisions", headers=bearer(viewer_key)).json()["items"] == []
    )


def test_decisions_expose_untrusted_text_as_plain_strings(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    agent_run(
        db,
        "macro",
        "COMPLETED",
        -1,
        {"regime": "RANGING", "evidence": ["<script>alert(1)</script>"]},
    )
    (item,) = client.get("/v1/agents/macro/decisions", headers=bearer(viewer_key)).json()["items"]
    assert item["evidence"] == ["<script>alert(1)</script>"]  # data, escaped by the client


def test_unknown_agent_is_404_and_runs_route_still_works(
    client: TestClient, viewer_key: str
) -> None:
    assert client.get("/v1/agents/nope", headers=bearer(viewer_key)).status_code == 404
    assert client.get("/v1/agents/runs", headers=bearer(viewer_key)).status_code == 200


def test_viewer_cannot_propose_or_review(
    client: TestClient, viewer_key: str, operator_key: str
) -> None:
    assert post(client, "/v1/agents/macro/config-proposals", viewer_key, NEW).status_code == 403
    created = post(client, "/v1/agents/macro/config-proposals", operator_key, NEW)
    pid = created.json()["id"]
    assert (
        post(
            client, f"/v1/agent-config-proposals/{pid}/review", viewer_key, {"approve": True}
        ).status_code
        == 403
    )


def test_operator_proposes_then_approves_and_pending_shows(
    client: TestClient, viewer_key: str, operator_key: str
) -> None:
    created = post(client, "/v1/agents/macro/config-proposals", operator_key, NEW)
    assert created.status_code == 201
    body = created.json()
    assert (body["status"], body["changes"], body["base"]) == (
        "PENDING_REVIEW", {"run_every_minutes": 15}, {"run_every_minutes": 30}
    )  # fmt: skip
    done = post(
        client,
        f"/v1/agent-config-proposals/{body['id']}/review",
        operator_key,
        {"approve": True},
        "k2",
    )
    assert (done.status_code, done.json()["status"]) == (200, "APPROVED")
    detail = client.get("/v1/agents/macro", headers=bearer(viewer_key)).json()
    field = {f["name"]: f for f in detail["config"]}["run_every_minutes"]
    assert (field["value"], field["pending"]) == (30, 15)  # approved, applies after the session
    listed = client.get("/v1/agents/macro/config-proposals", headers=bearer(viewer_key)).json()
    assert [p["status"] for p in listed["items"]] == ["APPROVED"]


def test_locked_and_invalid_edits_are_422(client: TestClient, operator_key: str) -> None:
    locked = post(client, "/v1/agents/macro/config-proposals", operator_key,
                  {"changes": {"llm": {}}, "rationale": "x"})  # fmt: skip
    assert locked.status_code == 422 and locked.json()["code"] == "INVALID_AGENT_CONFIG"
    cross = post(client, "/v1/agents/macro/config-proposals", operator_key,
                 {"changes": {"ttl_minutes": 20}, "rationale": "x"}, "k2")  # fmt: skip
    assert cross.status_code == 422


def test_unknown_agent_proposal_is_404_and_key_is_required(
    client: TestClient, operator_key: str
) -> None:
    assert post(client, "/v1/agents/nope/config-proposals", operator_key, NEW).status_code == 404
    assert (
        post(client, "/v1/agents/macro/config-proposals", operator_key, NEW, idem=None).status_code
        == 400
    )


def test_suggestion_sourced_proposal_must_match_a_proposable_suggestion(
    client: TestClient, operator_key: str
) -> None:
    body = {**NEW, "source": "SUGGESTION"}
    refused = post(client, "/v1/agents/macro/config-proposals", operator_key, body)
    assert refused.status_code == 422  # no proposable suggestion exists (no data)


def test_idempotent_replay_does_not_create_a_second_proposal(
    client: TestClient, db: Session, operator_key: str
) -> None:
    first = post(client, "/v1/agents/macro/config-proposals", operator_key, NEW)
    second = post(client, "/v1/agents/macro/config-proposals", operator_key, NEW)
    assert first.json() == second.json()
    db.rollback()
    assert db.execute(text("select count(*) from agent_config_proposals")).scalar_one() == 1
