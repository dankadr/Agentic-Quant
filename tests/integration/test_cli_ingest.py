import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from trading.cli import app


def test_ingest_csv_loads_fixture_and_reports(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(
        app,
        [
            "ingest-csv",
            "--path", "tests/fixtures/replay/2026-10-19",
            "--start", "2026-10-19T13:30:00Z",
            "--end", "2026-10-19T20:00:00Z",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["bars"]["inserted"] == 1170
    assert report["quotes"]["inserted"] == 1170
    assert db.execute(text("select count(*) from market_bars")).scalar_one() == 1170


def test_ingest_csv_rejects_naive_start(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(
        app,
        ["ingest-csv", "--path", "tests/fixtures/replay/2026-10-19",
         "--start", "2026-10-19T13:30:00", "--end", "2026-10-19T20:00:00Z"],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "timezone" in result.output.lower() or "naive" in result.output.lower()
