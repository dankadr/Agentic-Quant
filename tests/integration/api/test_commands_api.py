from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.integration.api.conftest import bearer


def _post(client: TestClient, path: str, key: str, *, version: int, idem: str):  # type: ignore[no-untyped-def]
    return client.post(
        path,
        headers={**bearer(key), "Idempotency-Key": idem},
        json={"reason": "drill", "expected_version": version},
    )


def test_flatten_is_accepted_and_tracked(
    client: TestClient,
    operator_key: str,
    viewer_key: str,
    make_ready: Callable[[], None],
) -> None:
    # Flatten is refused while the risk state is UNKNOWN, so start from a reconciled, NORMAL state.
    make_ready()
    version = client.get("/v1/system/status", headers=bearer(viewer_key)).json()["version"]
    accepted = _post(client, "/v1/control/flatten", operator_key, version=version, idem="flat-1")
    assert accepted.status_code == 202
    body = accepted.json()
    assert body["state"] == "FLATTENING"
    assert body["kind"] == "FLATTEN"

    status = client.get(f"/v1/control/commands/{body['command_id']}", headers=bearer(viewer_key))
    assert status.status_code == 200
    assert status.json()["status"] == "REQUESTED"


def test_only_operators_may_issue_commands(client: TestClient, viewer_key: str) -> None:
    for path in ("stop-entries", "cancel-open-orders", "flatten"):
        response = _post(client, f"/v1/control/{path}", viewer_key, version=1, idem=f"v-{path}")
        assert response.status_code == 403


def test_a_stale_version_is_refused_without_changing_state(
    client: TestClient, operator_key: str, viewer_key: str
) -> None:
    response = _post(client, "/v1/control/stop-entries", operator_key, version=99, idem="stale")
    assert response.status_code == 409
    status = client.get("/v1/system/status", headers=bearer(viewer_key)).json()
    assert status["trading_state"] == "PAUSED"


def test_unknown_command_is_404(client: TestClient, viewer_key: str) -> None:
    response = client.get(
        "/v1/control/commands/00000000-0000-4000-8000-000000000000", headers=bearer(viewer_key)
    )
    assert response.status_code == 404


def test_trading_readiness_says_why_entries_are_refused(
    client: TestClient, viewer_key: str, make_ready: Callable[[], None]
) -> None:
    before = client.get("/v1/health/trading", headers=bearer(viewer_key)).json()
    assert before["entries_permitted"] is False
    assert "EXECUTOR_LEASE_MISSING" in before["reasons"]

    make_ready()
    after = client.get("/v1/health/trading", headers=bearer(viewer_key)).json()
    assert after["entries_permitted"] is False  # still PAUSED
    assert "TRADING_PAUSED" in after["reasons"]
