"""Typed configuration. Live mode and forbidden instruments cannot be expressed here."""

import socket
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from trading.core.enums import AssetType
from trading.core.errors import TradingError
from trading.core.hashing import content_hash
from trading.core.money import NoFloatDecimal


class ConfigError(TradingError):
    """Invalid or missing configuration. The message names the offending file."""


Dec = NoFloatDecimal


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SystemConfig(_Frozen):
    mode: Literal["PAPER_ONLY"]
    timezone: Literal["America/New_York"]
    starting_cash_usd: Dec
    require_fresh_market_data: bool


class UniverseEntry(_Frozen):
    symbol: str
    asset_type: AssetType
    sector: str
    tick_size: Dec
    lot_size: Dec
    fractional_eligible: bool


class RiskPolicy(_Frozen):
    max_gross_exposure_pct: Dec
    max_symbol_exposure_pct: Dec
    max_sector_exposure_pct: Dec
    max_daily_loss_pct: Dec
    max_drawdown_pct: Dec
    max_new_entries_per_day: int
    max_open_symbols: int
    min_cash_reserve_pct: Dec
    allow_margin: Literal[False]
    allow_short: Literal[False]
    allow_options: Literal[False]
    allow_crypto: Literal[False]
    max_bar_age_seconds: int = 90
    entry_cutoff_minutes_before_close: int = 15
    max_spread_bps: Dec
    unknown_order_grace_seconds: int = 30


class PaperBrokerConfig(_Frozen):
    latency_ms: int
    slippage_bps: Dec
    max_participation_rate: Dec
    quote_max_age_seconds: int
    commission_per_order_usd: Dec
    sell_fee_rate: Dec
    taf_per_share_usd: Dec
    taf_max_per_trade_usd: Dec
    fractional_shares: bool
    fractional_increment: Dec
    settlement_days: int
    use_unsettled_funds: Literal[False]
    seed: int


class StrategyConfig(_Frozen):
    enabled: bool = True
    allocation_pct: Dec
    shadow_only: bool = False
    params: Mapping[str, Any] = {}


class _SystemFile(_Frozen):
    system: SystemConfig
    universe: tuple[UniverseEntry, ...]


class _RiskFile(_Frozen):
    risk: RiskPolicy


class _StrategiesFile(_Frozen):
    strategies: Mapping[str, StrategyConfig]

    @model_validator(mode="after")
    def _allocations_fit(self) -> "_StrategiesFile":
        total = sum((s.allocation_pct for s in self.strategies.values()), Decimal(0))
        if total > Decimal(1):
            raise ValueError(f"allocation_pct sums to {total}, which exceeds 1")
        return self


class _BrokerFile(_Frozen):
    paper_broker: PaperBrokerConfig


class AppConfig(_Frozen):
    system: SystemConfig
    universe: tuple[UniverseEntry, ...]
    risk: RiskPolicy
    paper_broker: PaperBrokerConfig
    strategies: Mapping[str, StrategyConfig]

    @property
    def limits_version(self) -> str:
        return content_hash(self.risk.model_dump())[:16]


def _load[T: BaseModel](config_dir: Path, filename: str, model: type[T]) -> T:
    path = config_dir / filename
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"{filename}: cannot read: {exc}") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{filename}: {exc}") from exc


def load_config(config_dir: Path) -> AppConfig:
    system = _load(config_dir, "system.yaml", _SystemFile)
    risk = _load(config_dir, "risk.yaml", _RiskFile)
    strategies = _load(config_dir, "strategies.yaml", _StrategiesFile)
    broker = _load(config_dir, "paper_broker.yaml", _BrokerFile)
    return AppConfig(
        system=system.system,
        universe=system.universe,
        risk=risk.risk,
        paper_broker=broker.paper_broker,
        strategies=strategies.strategies,
    )


def read_secret(path: Path) -> str:
    return path.read_text().rstrip("\n")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TRADING_", extra="ignore")

    config_dir: Path = Path("configs")
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "trading"
    db_user: str = "trading_app"
    db_password_file: Path | None = None
    database_url: str | None = None
    log_level: str = "INFO"
    log_json: bool = True
    worker_id: str = socket.gethostname()
    metrics_port: int | None = None
    alpaca_key_id_file: Path | None = None  # market-data keys only; never trading keys
    alpaca_secret_file: Path | None = None

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        auth = quote(self.db_user, safe="")
        if self.db_password_file is not None:
            auth += ":" + quote(read_secret(self.db_password_file), safe="")
        return f"postgresql+psycopg://{auth}@{self.db_host}:{self.db_port}/{self.db_name}"
