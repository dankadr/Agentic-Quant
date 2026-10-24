from pathlib import Path

import pytest

from trading.cli import _alpaca_provider
from trading.core.config import Settings


def test_missing_or_empty_key_files_mean_not_configured(tmp_path: Path) -> None:
    key, secret = tmp_path / "id", tmp_path / "sec"
    assert _alpaca_provider(Settings(alpaca_key_id_file=key, alpaca_secret_file=secret)) is None
    key.write_text("")
    secret.write_text("\n")
    assert _alpaca_provider(Settings(alpaca_key_id_file=key, alpaca_secret_file=secret)) is None


def test_configured_key_files_build_the_data_only_provider(tmp_path: Path) -> None:
    key, secret = tmp_path / "id", tmp_path / "sec"
    key.write_text("PKX\n")
    secret.write_text("shh\n")
    provider = _alpaca_provider(Settings(alpaca_key_id_file=key, alpaca_secret_file=secret))
    assert provider is not None
    assert provider.name == "alpaca-iex"
    assert "shh" not in repr(provider)


def test_ingest_alpaca_without_keys_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from trading.cli import app

    monkeypatch.chdir(tmp_path)  # no ./secrets here
    result = CliRunner().invoke(
        app, ["ingest-alpaca", "--start", "2026-10-23T14:30:00Z", "--end", "2026-10-23T14:31:00Z"]
    )
    assert result.exit_code == 1
