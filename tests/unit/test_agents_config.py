from pathlib import Path

import pytest

from trading.core.config import AgentsConfig, load_config


def test_shipped_agents_config_is_valid() -> None:
    cfg = load_config(Path("configs"))
    assert cfg.agents.macro.enabled is True
    assert cfg.agents.macro.required is False  # PoC choice, see configs/agents.yaml
    assert cfg.agents.evaluator.enabled is True
    assert cfg.agents.llm.base_url.startswith("https://")
    assert set(cfg.agents.macro.dependent_strategies) <= set(cfg.strategies)


def test_a_missing_agents_file_means_no_agents(tmp_path: Path) -> None:
    for name in ("system.yaml", "risk.yaml", "strategies.yaml", "paper_broker.yaml"):
        (tmp_path / name).write_text((Path("configs") / name).read_text())
    assert load_config(tmp_path).agents.macro.enabled is False


def test_ttl_must_outlive_the_run_interval() -> None:
    with pytest.raises(ValueError, match="ttl"):
        AgentsConfig.model_validate({"macro": {"ttl_minutes": 10, "run_every_minutes": 30}})


def test_llm_must_be_https_unless_local() -> None:
    with pytest.raises(ValueError, match="https"):
        AgentsConfig.model_validate({"llm": {"base_url": "http://example.com/v1", "model": "m"}})
    ok = AgentsConfig.model_validate(
        {"llm": {"base_url": "http://127.0.0.1:8888/v1", "model": "m"}}
    )
    assert ok.llm.model == "m"
