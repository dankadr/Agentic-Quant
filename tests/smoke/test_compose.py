"""Acceptance test 1: a clean `docker compose up --build` is healthy without broker credentials."""

import http.client
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
READY_URL = "http://127.0.0.1:8000/v1/health/ready"
# The smoke stack writes to a throwaway folder, never to the operator's ./data: a test run must not
# be able to reach real database or document files. Teardown never removes volumes or host folders.
SMOKE_DATA_DIR = tempfile.mkdtemp(prefix="trading-smoke-data-")


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TRADING_DATA_DIR": SMOKE_DATA_DIR}
    return subprocess.run(  # noqa: S603
        args, cwd=ROOT, capture_output=True, text=True, check=check, timeout=900, env=env
    )


@pytest.mark.smoke
def test_compose_up_healthy() -> None:
    _run("./scripts/init-secrets.sh")
    config = _run("docker", "compose", "config").stdout
    assert "broker" not in config.lower()  # no broker env vars or secrets anywhere
    try:
        _run("docker", "compose", "up", "-d", "--build")
        body = ""
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(READY_URL, timeout=3) as r:
                    body = r.read().decode()
                    if r.status == 200:
                        break
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                ConnectionError,
                TimeoutError,
            ):
                pass  # the port can accept connections before the app is listening
            time.sleep(2)
        if '"status":"ready"' not in body.replace(" ", ""):
            logs = _run("docker", "compose", "logs", "--no-color", "--tail", "40", check=False)
            print(logs.stdout[-4000:], logs.stderr[-1000:])
        assert '"status":"ready"' in body.replace(" ", ""), body
        assert '"market_data":"not_configured"' in body.replace(" ", "")
        assert '"executor_lease":"held"' in body.replace(" ", "")
        assert '"database":"ok"' in body.replace(" ", "")
    finally:
        _run("docker", "compose", "down", check=False)  # keeps volumes and host folders


def _call(
    method: str, path: str, key: str, body: dict | None = None, idem: str | None = None
) -> tuple[int, dict]:  # type: ignore[type-arg]
    import json

    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = idem
    request = urllib.request.Request(
        f"http://127.0.0.1:8000{path}", method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=10) as r:  # noqa: S310
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


@pytest.mark.smoke
def test_compose_operator_flow() -> None:
    """Real containers: create an API key, read status, resume and pause paper trading."""
    _run("./scripts/init-secrets.sh")
    try:
        _run("docker", "compose", "up", "-d", "--build")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(READY_URL, timeout=3) as r:
                    if r.status == 200:
                        break
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                ConnectionError,
                TimeoutError,
            ):
                pass
            time.sleep(2)
        created = _run("docker", "compose", "run", "--rm", "-T", "api", "admin", "create-api-key",
                       "--name", "smoke", "--role", "operator")  # fmt: skip
        key = created.stdout.strip().splitlines()[-1]
        assert key.startswith("trk_")

        status, body = _call("GET", "/v1/system/status", key)
        assert status == 200
        assert body["mode"] == "PAPER_ONLY"
        assert body["trading_state"] == "PAUSED"
        assert body["risk_state"] == "NORMAL"  # the worker reconciled on start
        assert _call("GET", "/v1/system/status", "trk_wrong")[0] == 401

        version = body["version"]
        status, resumed = _call("POST", "/v1/control/resume-paper", key,
                                {"reason": "smoke", "expected_version": version}, idem="smoke-1")  # fmt: skip
        assert (status, resumed["state"]) == (200, "PAPER_RUNNING")
        status, paused = _call("POST", "/v1/control/pause", key,
                               {"reason": "smoke", "expected_version": resumed["version"]}, idem="smoke-2")  # fmt: skip
        assert (status, paused["state"]) == (200, "PAUSED")
        assert _call("GET", "/v1/audit", key)[1]["items"], "control actions must be audited"
    finally:
        _run("docker", "compose", "down", check=False)  # keeps volumes and host folders
