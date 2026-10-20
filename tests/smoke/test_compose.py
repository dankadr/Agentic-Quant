"""Acceptance test 1: a clean `docker compose up --build` is healthy without broker credentials."""

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
READY_URL = "http://127.0.0.1:8000/v1/health/ready"


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=check, timeout=900)  # noqa: S603


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
            except urllib.error.URLError:
                pass
            time.sleep(2)
        assert '"status":"ready"' in body.replace(" ", ""), body
        assert '"market_data":"not_configured"' in body.replace(" ", "")
        assert '"executor_lease":"held"' in body.replace(" ", "")
        assert '"database":"ok"' in body.replace(" ", "")
    finally:
        _run("docker", "compose", "down", "-v", check=False)
