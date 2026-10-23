from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from trading.api.middleware import MetricsMiddleware


def _samples() -> list[dict[str, str]]:
    return [
        s.labels
        for metric in REGISTRY.collect()
        if metric.name == "trading_http_requests"
        for s in metric.samples
        if s.name == "trading_http_requests_total"
    ]


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "1"}

    return app


def test_unknown_http_methods_collapse_to_other() -> None:
    client = TestClient(_app())
    for method in ("FOO1", "FOO2", "FOO3"):
        client.request(method, "/ping")
    methods = {labels["method"] for labels in _samples()}
    assert not {"FOO1", "FOO2", "FOO3"} & methods
    assert "OTHER" in methods


def test_standard_methods_keep_their_label() -> None:
    TestClient(_app()).get("/ping")
    assert {"method": "GET", "route": "/ping", "status": "200"} in _samples()


def test_unmatched_paths_share_one_route_label() -> None:
    client = TestClient(_app())
    client.get("/random-1")
    client.get("/random-2")
    routes = {labels["route"] for labels in _samples()}
    assert not {"/random-1", "/random-2"} & routes
