from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.integration.api.conftest import bearer
from trading.api.auth import Role, generate_api_key, hash_api_key


def test_generated_keys_have_prefix_and_entropy() -> None:
    a, b = generate_api_key(), generate_api_key()
    assert a.startswith("trk_")
    assert len(a) >= 40
    assert a != b
    assert hash_api_key(a) != a
    assert len(hash_api_key(a)) == 64


def test_missing_token_401(client: TestClient) -> None:
    r = client.get("/v1/system/status")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.json()["code"] == "UNAUTHENTICATED"
    assert r.headers["content-type"].startswith("application/problem+json")


def test_malformed_and_unknown_tokens_401(client: TestClient) -> None:
    assert (
        client.get("/v1/system/status", headers={"Authorization": "Basic abc"}).status_code == 401
    )
    assert client.get("/v1/system/status", headers=bearer("trk_nope")).status_code == 401
    assert client.get("/v1/system/status", headers={"Authorization": "Bearer "}).status_code == 401


def test_viewer_and_operator_can_read_status(
    client: TestClient, viewer_key: str, operator_key: str
) -> None:
    assert client.get("/v1/system/status", headers=bearer(viewer_key)).status_code == 200
    assert client.get("/v1/system/status", headers=bearer(operator_key)).status_code == 200


def test_viewer_cannot_pause(client: TestClient, viewer_key: str) -> None:
    r = client.post(
        "/v1/control/pause",
        headers={**bearer(viewer_key), "Idempotency-Key": "k"},
        json={"reason": "x", "expected_version": 1},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"


def test_unauthenticated_cannot_pause(client: TestClient) -> None:
    r = client.post(
        "/v1/control/pause",
        headers={"Idempotency-Key": "k"},
        json={"reason": "x", "expected_version": 1},
    )
    assert r.status_code == 401


def test_disabled_principal_rejected(
    client: TestClient, db: Session, make_key: Callable[[str, Role], str]
) -> None:
    key = make_key("gone", Role.OPERATOR)
    assert client.get("/v1/system/status", headers=bearer(key)).status_code == 200
    db.execute(text("update principals set disabled_at = now() where name = 'gone'"))
    db.commit()
    assert client.get("/v1/system/status", headers=bearer(key)).status_code == 401


def test_only_key_hash_is_stored(db: Session, operator_key: str) -> None:
    stored = db.execute(text("select api_key_hash from principals where name = 'op1'")).scalar_one()
    assert stored == hash_api_key(operator_key)
    assert operator_key not in stored
