import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from trading.api.auth import hash_api_key
from trading.cli import app


def test_cli_create_api_key_prints_key_once(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(
        app, ["admin", "create-api-key", "--name", "me", "--role", "operator"]
    )
    assert result.exit_code == 0, result.output
    key = result.stdout.strip()
    assert key.startswith("trk_")
    assert "trk_" not in result.stderr
    stored = db.execute(text("select role, api_key_hash from principals where name='me'")).one()
    assert stored.role == "operator"
    assert stored.api_key_hash == hash_api_key(key)


def test_cli_duplicate_name_fails_without_traceback(
    db: Session, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    runner = CliRunner()
    assert (
        runner.invoke(
            app, ["admin", "create-api-key", "--name", "me", "--role", "viewer"]
        ).exit_code
        == 0
    )
    again = runner.invoke(app, ["admin", "create-api-key", "--name", "me", "--role", "viewer"])
    assert again.exit_code == 1
    assert "already exists" in again.stderr
    assert "Traceback" not in again.output


def test_cli_rejects_unknown_role(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", database_url)
    result = CliRunner().invoke(app, ["admin", "create-api-key", "--name", "x", "--role", "root"])
    assert result.exit_code != 0
