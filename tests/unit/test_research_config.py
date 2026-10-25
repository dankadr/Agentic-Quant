from pathlib import Path

from trading.core.config import load_config
from trading.quant.research_config import load_research_spaces


def test_shipped_research_grids_only_tune_known_strategy_parameters() -> None:
    cfg = load_config(Path("configs"))
    spaces = load_research_spaces(Path("configs"))
    assert set(spaces) == {"orb_v1", "vwap_v1"}
    for strategy_id, space in spaces.items():
        known = set(cfg.strategies[strategy_id].params)
        assert set(space) <= known
        assert all(len(values) >= 2 for values in space.values())


def test_a_missing_file_means_no_research(tmp_path: Path) -> None:
    assert load_research_spaces(tmp_path) == {}
