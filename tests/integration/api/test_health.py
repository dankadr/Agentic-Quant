from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy.orm import Session

from trading.api.app import create_app
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.db.engine import make_engine, make_session_factory
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease


def test_live_ok_without_auth(client: TestClient) -> None:
    r = client.get("/v1/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ready_503_without_executor_lease(client: TestClient) -> None:
    r = client.get("/v1/health/ready")
    assert r.status_code == 503
    assert r.json() == {
        "status": "not_ready",
        "database": "ok",
        "market_data": "not_configured",
        "executor_lease": "missing",
    }


def test_ready_200_with_lease(client: TestClient, db: Session, clock: ManualClock) -> None:
    acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=timedelta(seconds=30))
    db.commit()
    r = client.get("/v1/health/ready")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ready",
        "database": "ok",
        "market_data": "not_configured",
        "executor_lease": "held",
    }


def test_ready_503_when_lease_expired(client: TestClient, db: Session, clock: ManualClock) -> None:
    acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=timedelta(seconds=30))
    db.commit()
    clock.advance(timedelta(seconds=30))
    assert client.get("/v1/health/ready").status_code == 503


def test_ready_503_when_db_down(clock: ManualClock) -> None:
    dead = make_session_factory(make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none"))
    app = create_app(
        settings=Settings(), config=load_config(Path("configs")), session_factory=dead, clock=clock
    )
    r = TestClient(app, raise_server_exceptions=False).get("/v1/health/ready")
    assert r.status_code == 503
    assert r.json()["database"] == "error"
    assert r.json()["executor_lease"] == "missing"
    assert r.json()["status"] == "not_ready"


def test_live_stays_ok_when_db_down(clock: ManualClock) -> None:
    dead = make_session_factory(make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none"))
    app = create_app(
        settings=Settings(), config=load_config(Path("configs")), session_factory=dead, clock=clock
    )
    assert TestClient(app).get("/v1/health/live").status_code == 200


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    r = client.get("/v1/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")


def test_http_metrics_use_route_template_labels(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/v1/health/live", "status": "200"}
    before = REGISTRY.get_sample_value("trading_http_requests_total", labels) or 0
    client.get("/v1/health/live")
    assert REGISTRY.get_sample_value("trading_http_requests_total", labels) == before + 1
