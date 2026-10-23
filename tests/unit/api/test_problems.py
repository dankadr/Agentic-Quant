import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from trading.api.middleware import RequestIdMiddleware
from trading.api.problems import ApiProblem, register_handlers
from trading.control.state import VersionConflict


class Body(BaseModel):
    qty: int


def _app() -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.get("/problem")
    def problem() -> None:
        raise ApiProblem(409, "SOME_CONFLICT", "Conflict", detail="nope", extra={"k": "v"})

    @app.get("/conflict")
    def conflict() -> None:
        raise VersionConflict(7)

    @app.post("/validate")
    def validate(body: Body) -> dict[str, int]:
        return {"qty": body.qty}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals trk_leak")

    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_api_problem_shape(client: TestClient) -> None:
    r = client.get("/problem", headers={"X-Request-ID": "req-1"})
    assert r.status_code == 409
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["type"] == "urn:trading:problem:SOME_CONFLICT"
    assert body["title"] == "Conflict"
    assert body["status"] == 409
    assert body["detail"] == "nope"
    assert body["instance"] == "/problem"
    assert body["code"] == "SOME_CONFLICT"
    assert body["request_id"] == "req-1"
    assert body["k"] == "v"


def test_version_conflict_is_409_with_current_version(client: TestClient) -> None:
    r = client.get("/conflict")
    assert r.status_code == 409
    assert r.json()["code"] == "VERSION_CONFLICT"
    assert r.json()["current_version"] == 7


def test_validation_error_is_422_without_echoing_input(client: TestClient) -> None:
    r = client.post("/validate", json={"qty": "hunter2-not-an-int"})
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert body["errors"][0]["loc"] == ["body", "qty"]
    assert "hunter2" not in r.text


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "NOT_FOUND"


def test_unhandled_exception_is_500_without_internals(client: TestClient) -> None:
    r = client.get("/boom", headers={"X-Request-ID": "req-9"})
    assert r.status_code == 500
    assert r.json()["code"] == "INTERNAL"
    assert "secret internals" not in r.text
    assert "trk_leak" not in r.text
    assert r.headers["x-request-id"] == "req-9"


def test_request_id_generated_when_missing_or_invalid(client: TestClient) -> None:
    generated = client.get("/problem").headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f-]{36}", generated)
    replaced = client.get("/problem", headers={"X-Request-ID": "bad id!"}).headers["x-request-id"]
    assert replaced != "bad id!"
    assert re.fullmatch(r"[0-9a-f-]{36}", replaced)
    assert (
        client.get("/problem", headers={"X-Request-ID": "ok-1.2_3"}).headers["x-request-id"]
        == "ok-1.2_3"
    )
