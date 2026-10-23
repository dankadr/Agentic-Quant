import json

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from trading.cli import app


def test_replay_command_prints_a_result(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(
        app,
        ["replay", "--fixtures", "tests/fixtures/replay/2026-10-19", "--date", "2026-10-19",
         "--script", "tests/fixtures/replay/scripts/expired.yaml"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["orders"] == 0
    assert report["decisions"] == 1
    assert len(report["result_hash"]) == 64
    assert report["final_equity"] == "1000.0000"


def test_replay_command_rejects_a_non_session_date(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(
        app, ["replay", "--fixtures", "tests/fixtures/replay/2026-10-19", "--date", "2026-10-24"]
    )
    assert result.exit_code == 1
    assert "not a trading session" in result.stderr
