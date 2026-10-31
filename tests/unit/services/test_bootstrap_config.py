from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer

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


def test_research_worker_starts_from_the_effective_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading.services import research

    base = load_config(Path("configs"))
    fake_settings = SimpleNamespace(worker_id="r1", document_store_dir=Path("unused"))
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "_bootstrap", lambda service: (fake_settings, base, object(), "v1"))
    monkeypatch.setattr(cli, "_calendar", lambda: None)
    monkeypatch.setattr(cli, "_market_data_provider_name", lambda settings: "none")
    monkeypatch.setattr(cli, "_llm_client", lambda settings, config: None)
    monkeypatch.setattr(cli, "_news_source", lambda settings, config: None)
    monkeypatch.setattr(cli, "BlobStore", lambda path: None)
    monkeypatch.setattr(cli, "new_holder_id", lambda worker_id: "r1")
    monkeypatch.setattr(cli, "_stop_on_signals", lambda: None)
    monkeypatch.setattr(research, "ResearchContext", lambda **kwargs: None)
    monkeypatch.setattr(research, "build_research_handlers", lambda ctx: {})
    monkeypatch.setattr(research, "run_research_worker", fake_run)

    with pytest.raises(typer.Exit):
        cli.research_worker()

    assert seen["config_version"] == "v1"
