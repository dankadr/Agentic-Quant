import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from trading.core.config import AppConfig, ConfigError, Settings, load_config, read_secret


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    dest = tmp_path / "configs"
    shutil.copytree("configs", dest)
    return dest


def edit(path: Path, keys: list[str], value: Any) -> None:
    data = yaml.safe_load(path.read_text())
    node = data
    for k in keys[:-1]:
        node = node[k]
    node[keys[-1]] = value
    path.write_text(yaml.safe_dump(data))


def test_loads_repo_configs() -> None:
    cfg = load_config(Path("configs"))
    assert cfg.system.mode == "PAPER_ONLY"
    assert cfg.system.starting_cash_usd == Decimal("1000.00")
    assert cfg.risk.max_symbol_exposure_pct == Decimal("0.10")
    assert cfg.risk.max_new_entries_per_day == 10
    assert cfg.risk.max_open_symbols == 3
    assert len(cfg.limits_version) == 16
    assert {u.symbol for u in cfg.universe} == {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "TQQQ",
        "AAPL",
        "MSFT",
        "NVDA",
        "GOOGL",
        "META",
        "AMZN",
    }
    assert cfg.strategies["orb_v1"].allocation_pct == Decimal("0.20")
    assert cfg.strategies["consensus_shadow"].shadow_only is True


def test_live_mode_rejected(tmp_config_dir: Path) -> None:
    edit(tmp_config_dir / "system.yaml", ["system", "mode"], "LIVE")
    with pytest.raises(ConfigError, match=r"system\.yaml"):
        load_config(tmp_config_dir)


@pytest.mark.parametrize("flag", ["allow_margin", "allow_short", "allow_options", "allow_crypto"])
def test_forbidden_risk_flags_rejected(tmp_config_dir: Path, flag: str) -> None:
    edit(tmp_config_dir / "risk.yaml", ["risk", flag], True)
    with pytest.raises(ConfigError, match=r"risk\.yaml"):
        load_config(tmp_config_dir)


def test_float_values_rejected(tmp_config_dir: Path) -> None:
    edit(tmp_config_dir / "system.yaml", ["system", "starting_cash_usd"], 1000.0)
    with pytest.raises(ConfigError, match=r"system\.yaml"):
        load_config(tmp_config_dir)


def test_allocation_sum_over_one_rejected(tmp_config_dir: Path) -> None:
    edit(tmp_config_dir / "strategies.yaml", ["strategies", "orb_v1", "allocation_pct"], "0.90")
    with pytest.raises(ConfigError, match=r"strategies\.yaml"):
        load_config(tmp_config_dir)


def test_unknown_key_rejected(tmp_config_dir: Path) -> None:
    edit(tmp_config_dir / "risk.yaml", ["risk", "disable_risk"], True)
    with pytest.raises(ConfigError, match=r"risk\.yaml"):
        load_config(tmp_config_dir)


def test_use_unsettled_funds_must_be_false(tmp_config_dir: Path) -> None:
    edit(tmp_config_dir / "paper_broker.yaml", ["paper_broker", "use_unsettled_funds"], True)
    with pytest.raises(ConfigError, match=r"paper_broker\.yaml"):
        load_config(tmp_config_dir)


def test_missing_file_is_config_error(tmp_config_dir: Path) -> None:
    (tmp_config_dir / "risk.yaml").unlink()
    with pytest.raises(ConfigError, match=r"risk\.yaml"):
        load_config(tmp_config_dir)


def test_limits_version_changes_with_risk(tmp_config_dir: Path) -> None:
    before = load_config(tmp_config_dir).limits_version
    edit(tmp_config_dir / "risk.yaml", ["risk", "max_open_symbols"], 2)
    assert load_config(tmp_config_dir).limits_version != before


def test_config_is_frozen() -> None:
    cfg: AppConfig = load_config(Path("configs"))
    with pytest.raises(ValueError, match="frozen"):
        cfg.risk.max_open_symbols = 99  # type: ignore[misc]


def test_settings_reads_secret_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pw = tmp_path / "pw"
    pw.write_text("s3cret\n")
    monkeypatch.setenv("TRADING_DB_HOST", "db")
    monkeypatch.setenv("TRADING_DB_NAME", "trading")
    monkeypatch.setenv("TRADING_DB_USER", "trading_app")
    monkeypatch.setenv("TRADING_DB_PASSWORD_FILE", str(pw))
    s = Settings()
    assert read_secret(pw) == "s3cret"
    assert s.resolved_database_url() == "postgresql+psycopg://trading_app:s3cret@db:5432/trading"


def test_settings_database_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_DATABASE_URL", "postgresql+psycopg://u:p@h/d")
    assert Settings().resolved_database_url() == "postgresql+psycopg://u:p@h/d"


def test_strategy_params_and_sleeves_in_repo_config() -> None:
    cfg = load_config(Path("configs"))
    orb, vwap = cfg.strategies["orb_v1"], cfg.strategies["vwap_v1"]
    assert (orb.allocation_pct, vwap.allocation_pct) == (
        Decimal("0.20"),
        Decimal("0.15"),
    )  # technical sleeve 35%
    assert orb.params["opening_range_minutes"] == 15
    assert vwap.params["lookback_bars"] == 30
    assert cfg.strategies["eod_flatten"].params["flatten_minutes_before_close"] == 10
    assert cfg.strategies["eod_flatten"].enabled and not cfg.strategies["eod_flatten"].shadow_only
    assert sum(s.allocation_pct for s in cfg.strategies.values()) == Decimal(1)  # spec sleeves
    assert cfg.strategies["consensus_shadow"].allocation_pct == Decimal("0.20")
    assert cfg.strategies["cash_reserve"].allocation_pct == Decimal("0.10")
    assert cfg.strategies["fundamental_watchlist"].params == {}
