from typer.testing import CliRunner

import trading
from trading.cli import app


def test_version_constant() -> None:
    assert trading.__version__ == "0.1.0"


def test_cli_version() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.1.0"
