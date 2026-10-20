from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.integration.api.conftest import bearer
from trading.core.clock import ManualClock
from trading.monitoring.audit import record_audit


def _seed(db: Session, clock: ManualClock, n: int) -> None:
    for i in range(n):
        clock.advance(timedelta(seconds=1))
        record_audit(
            db, actor="t", action=f"a{i}", target="x", request_id=None, before=None, after=None,
            result="ok", now=clock.now(),
        )  # fmt: skip
    db.commit()


def test_audit_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/audit").status_code == 401


def test_audit_paginates_newest_first(
    client: TestClient, viewer_key: str, db: Session, clock: ManualClock
) -> None:
    _seed(db, clock, 5)
    seen: list[str] = []
    cursor = None
    for _ in range(4):
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        body = client.get("/v1/audit", headers=bearer(viewer_key), params=params).json()
        seen += [i["action"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ["a4", "a3", "a2", "a1", "a0"]
    assert cursor is None


def test_audit_item_shape(
    client: TestClient, viewer_key: str, db: Session, clock: ManualClock
) -> None:
    _seed(db, clock, 1)
    item = client.get("/v1/audit", headers=bearer(viewer_key)).json()["items"][0]
    assert set(item) == {
        "id",
        "actor",
        "action",
        "target",
        "ts",
        "request_id",
        "before_hash",
        "after_hash",
        "result",
    }
    assert item["ts"].endswith("Z")


def test_audit_invalid_cursor_and_limits(client: TestClient, viewer_key: str) -> None:
    h = bearer(viewer_key)
    assert client.get("/v1/audit", headers=h, params={"cursor": "garbage"}).status_code == 422
    assert client.get("/v1/audit", headers=h, params={"limit": 0}).status_code == 422
    assert client.get("/v1/audit", headers=h, params={"limit": 201}).status_code == 422
