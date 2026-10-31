from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.api.conftest import bearer
from trading.control.commands import CommandStatus, finish_command, open_commands
from trading.core.clock import ManualClock


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


def _state(client: TestClient, key: str) -> tuple[str, int]:
    body = client.get("/v1/system/status", headers=bearer(key)).json()
    return body["trading_state"], body["version"]


def test_resume_is_refused_during_a_flatten(
    client: TestClient, operator_key: str, viewer_key: str, make_ready: Callable[[], None]
) -> None:
    """Review finding: resume used to start new entries while the system was still liquidating."""
    make_ready()
    _, version = _state(client, viewer_key)
    _post(client, "/v1/control/flatten", operator_key, version=version, idem="f-1")
    state, version = _state(client, viewer_key)
    assert state == "FLATTENING"

    refused = _post(client, "/v1/control/resume-paper", operator_key, version=version, idem="r-1")

    assert refused.status_code == 409
    assert refused.json()["code"] == "COMMAND_IN_PROGRESS"
    assert _state(client, viewer_key)[0] == "FLATTENING"


def test_resume_waits_for_an_open_command_then_works(
    client: TestClient,
    operator_key: str,
    viewer_key: str,
    make_ready: Callable[[], None],
    session_factory: sessionmaker[Session],
    clock: ManualClock,
) -> None:
    make_ready()
    _, version = _state(client, viewer_key)
    resumed = _post(client, "/v1/control/resume-paper", operator_key, version=version, idem="r-0")
    assert resumed.status_code == 200
    stop = _post(
        client, "/v1/control/stop-entries", operator_key, version=resumed.json()["version"],
        idem="s-1",
    )  # fmt: skip
    assert stop.status_code == 202
    state, version = _state(client, viewer_key)
    assert state == "EXIT_ONLY"

    refused = _post(client, "/v1/control/resume-paper", operator_key, version=version, idem="r-1")
    assert refused.status_code == 409
    assert refused.json()["open_commands"][0]["kind"] == "STOP_ENTRIES"

    with session_factory.begin() as s:
        for row in open_commands(s):
            finish_command(s, row, status=CommandStatus.COMPLETED, progress={}, now=clock.now())

    allowed = _post(client, "/v1/control/resume-paper", operator_key, version=version, idem="r-2")
    assert allowed.status_code == 200
    assert allowed.json()["state"] == "PAPER_RUNNING"


def test_pause_is_always_allowed_during_a_flatten(
    client: TestClient, operator_key: str, viewer_key: str, make_ready: Callable[[], None]
) -> None:
    make_ready()
    _, version = _state(client, viewer_key)
    _post(client, "/v1/control/flatten", operator_key, version=version, idem="f-1")
    _, version = _state(client, viewer_key)

    paused = _post(client, "/v1/control/pause", operator_key, version=version, idem="p-1")

    assert paused.status_code == 200
    assert paused.json()["state"] == "PAUSED"
