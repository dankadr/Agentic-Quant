from pathlib import Path
from typing import Any

import pytest

from trading import cli
from trading.core.config import AppConfig, load_config


def test_long_running_services_start_from_the_effective_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = load_config(Path("configs"))
    marker = base.model_copy()  # a distinct object standing for "the effective config"
    seen: dict[str, Any] = {}

    def fake_effective(config_dir: Path, session_factory: object) -> tuple[AppConfig, str | None]:
        seen["dir"], seen["factory"] = config_dir, session_factory
        return marker, "v1"

    sentinel = object()
    monkeypatch.setattr(cli, "load_effective_config", fake_effective)
    monkeypatch.setattr(cli, "make_engine", lambda url: object())
    monkeypatch.setattr(cli, "make_session_factory", lambda engine: sentinel)
    monkeypatch.setattr(cli, "start_metrics_server", lambda port: None)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)
    monkeypatch.setenv("TRADING_DATABASE_URL", "postgresql+psycopg://x:y@localhost/z")
    monkeypatch.setenv("TRADING_CONFIG_DIR", "configs")

    _, config, factory, version = cli._bootstrap("worker")

    assert (config is marker, version) == (True, "v1")
    assert seen == {"dir": Path("configs"), "factory": sentinel}
    assert factory is sentinel
