from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.api.conftest import bearer
from trading.api.auth import Role


def _post(
    client: TestClient,
    path: str,
    key: str,
    *,
    idem: str | None = "k1",
    version: int = 1,
    reason: str = "because",
    request_id: str | None = None,
):  # type: ignore[no-untyped-def]
    headers = bearer(key)
    if idem is not None:
        headers["Idempotency-Key"] = idem
    if request_id:
        headers["X-Request-ID"] = request_id
    return client.post(path, headers=headers, json={"reason": reason, "expected_version": version})


def _audit_count(db: Session, action: str) -> int:
    db.rollback()
    return db.execute(
        text("select count(*) from audit_events where action = :a"), {"a": action}
    ).scalar_one()


def test_status_initial(client: TestClient, viewer_key: str) -> None:
    body = client.get("/v1/system/status", headers=bearer(viewer_key)).json()
    release = body.pop("release")
    # The schema is fully migrated, and the config fingerprint is a real digest.
    assert release["schema_applied"] == release["schema_head"] == "0005"
    assert len(release["config_sha256"]) == 64
    assert body == {
        "mode": "PAPER_ONLY",
        "trading_enabled": False,
        "trading_state": "PAUSED",
        "risk_state": "UNKNOWN",
        "risk_reason": None,
        "version": 1,
        "session": {
            "date": "2026-10-19",
            "open_at": "2026-10-19T13:30:00Z",
            "close_at": "2026-10-19T20:00:00Z",
            "is_open": True,
            "is_half_day": False,
        },
        "last_data_at": None,
        "last_reconciled_at": None,
    }


def test_pause_requires_idempotency_key(client: TestClient, operator_key: str) -> None:
    r = _post(client, "/v1/control/pause", operator_key, idem=None)
    assert r.status_code == 400
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_overlong_idempotency_key_422(client: TestClient, operator_key: str) -> None:
    assert _post(client, "/v1/control/pause", operator_key, idem="k" * 129).status_code == 422


def test_pause_is_idempotent_and_audited_once(
    client: TestClient, operator_key: str, db: Session, make_ready: Callable[[], None]
) -> None:
    make_ready()  # risk NORMAL bumps version to 2
    r_resume = _post(client, "/v1/control/resume-paper", operator_key, idem="r", version=2)
    assert r_resume.status_code == 200
    r1 = _post(client, "/v1/control/pause", operator_key, idem="p1", version=3)
    r2 = _post(client, "/v1/control/pause", operator_key, idem="p1", version=3)
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == r2.json()
    assert r1.json()["state"] == "PAUSED"
    assert r2.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in r1.headers
    assert _audit_count(db, "trading_state.set") == 2  # one resume + one pause


def test_idempotency_key_reuse_with_different_body_422(
    client: TestClient, operator_key: str
) -> None:
    assert _post(client, "/v1/control/pause", operator_key, idem="k", reason="a").status_code == 200
    r = _post(client, "/v1/control/pause", operator_key, idem="k", reason="b")
    assert r.status_code == 422
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_idempotency_key_is_scoped_per_principal_and_route(
    client: TestClient, operator_key: str, make_key: Callable[[str, Role], str]
) -> None:
    other = make_key("op2", Role.OPERATOR)
    a = _post(client, "/v1/control/pause", operator_key, idem="same")
    b = _post(client, "/v1/control/pause", other, idem="same")
    assert a.status_code == b.status_code == 200
    assert "idempotent-replayed" not in b.headers


def test_failed_request_is_not_cached(client: TestClient, operator_key: str) -> None:
    stale = _post(client, "/v1/control/pause", operator_key, idem="k", version=99)
    assert stale.status_code == 409
    assert stale.json()["code"] == "VERSION_CONFLICT"
    assert stale.json()["current_version"] == 1
    ok = _post(client, "/v1/control/pause", operator_key, idem="k", version=1)
    assert ok.status_code == 200


def test_replay_returns_original_even_after_state_changed(
    client: TestClient, operator_key: str, make_ready: Callable[[], None]
) -> None:
    first = _post(client, "/v1/control/pause", operator_key, idem="p", version=1)
    make_ready()
    _post(client, "/v1/control/resume-paper", operator_key, idem="r", version=2)
    replay = _post(client, "/v1/control/pause", operator_key, idem="p", version=1)
    assert replay.json() == first.json()


def test_resume_refused_when_risk_unknown(
    client: TestClient,
    operator_key: str,
    session_factory,
    clock,  # type: ignore[no-untyped-def]
) -> None:
    from datetime import timedelta

    from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease

    with session_factory.begin() as s:
        acquire_lease(s, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=timedelta(hours=1))
    r = _post(client, "/v1/control/resume-paper", operator_key)
    assert r.status_code == 503
    assert r.json()["code"] == "NOT_READY"
    assert r.json()["checks"]["risk_state"] == "UNKNOWN"
    assert r.json()["checks"]["executor_lease"] == "held"


def test_resume_refused_without_executor_lease(client: TestClient, operator_key: str) -> None:
    r = _post(client, "/v1/control/resume-paper", operator_key)
    assert r.status_code == 503
    assert r.json()["checks"]["executor_lease"] == "missing"


def test_resume_succeeds_when_ready_and_status_reflects_it(
    client: TestClient, operator_key: str, make_ready: Callable[[], None]
) -> None:
    make_ready()
    r = _post(client, "/v1/control/resume-paper", operator_key, version=2)
    assert r.status_code == 200
    assert r.json()["state"] == "PAPER_RUNNING"
    assert r.json()["version"] == 3
    status = client.get("/v1/system/status", headers=bearer(operator_key)).json()
    assert status["trading_enabled"] is True
    assert status["trading_state"] == "PAPER_RUNNING"


def test_pause_is_always_allowed_even_when_not_ready(client: TestClient, operator_key: str) -> None:
    assert _post(client, "/v1/control/pause", operator_key).status_code == 200


def test_validation(client: TestClient, operator_key: str) -> None:
    headers = {**bearer(operator_key), "Idempotency-Key": "k"}
    assert client.post("/v1/control/pause", headers=headers, json={"reason": ""}).status_code == 422
    assert (
        client.post(
            "/v1/control/pause", headers=headers, json={"reason": "x", "expected_version": "a"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/control/pause", headers=headers, json={"reason": "r" * 501, "expected_version": 1}
        ).status_code
        == 422
    )


def test_control_actions_audited_with_actor_and_request_id(
    client: TestClient, operator_key: str, db: Session
) -> None:
    _post(client, "/v1/control/pause", operator_key, idem="a", request_id="req-77", reason="maint")
    db.rollback()
    row = db.execute(
        text(
            "select actor, request_id, details_json from audit_events"
            " where action = 'trading_state.set'"
        )
    ).one()
    assert row.actor == "op1"
    assert row.request_id == "req-77"
    assert row.details_json["reason"] == "maint"
