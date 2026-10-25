"""Typed configuration. Live mode and forbidden instruments cannot be expressed here."""

import socket
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
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


class LlmConfig(_Frozen):
    base_url: str = "https://chat.daria-e.com/v1"
    model: str = "unsloth/Qwen3.8-27B-GGUF"
    timeout_seconds: int = Field(default=120, gt=0, le=600)
    max_tokens: int = Field(default=1200, gt=0, le=16384)

    @model_validator(mode="after")
    def _https_unless_local(self) -> "LlmConfig":
        host = urlparse(self.base_url).hostname or ""
        if not self.base_url.startswith("https://") and host not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("llm base_url must use https unless it points at localhost")
        return self


class MacroAgentConfig(_Frozen):
    enabled: bool = False
    required: bool = True
    ttl_minutes: int = Field(default=60, gt=0, le=480)
    run_every_minutes: int = Field(default=30, gt=0, le=240)
    first_run_after_open_minutes: int = Field(default=16, ge=1, le=120)
    dependent_strategies: tuple[str, ...] = ("orb_v1", "vwap_v1")

    @model_validator(mode="after")
    def _ttl_outlives_interval(self) -> "MacroAgentConfig":
        if self.ttl_minutes <= self.run_every_minutes:
            raise ValueError("macro ttl_minutes must exceed run_every_minutes")
        return self


class EvaluatorAgentConfig(_Frozen):
    enabled: bool = False


class FundamentalAgentConfig(_Frozen):
    enabled: bool = False
    lookback_hours: int = Field(default=18, gt=0, le=72)
    max_headlines: int = Field(default=60, gt=0, le=200)
    ttl_hours: int = Field(default=20, gt=0, le=72)


class ConsensusAgentConfig(_Frozen):
    enabled: bool = False


class TechnicalAgentConfig(_Frozen):
    enabled: bool = False


class AgentsConfig(_Frozen):
    llm: LlmConfig = LlmConfig()
    macro: MacroAgentConfig = MacroAgentConfig()
    evaluator: EvaluatorAgentConfig = EvaluatorAgentConfig()
    fundamental: FundamentalAgentConfig = FundamentalAgentConfig()
    consensus: ConsensusAgentConfig = ConsensusAgentConfig()
    technical: TechnicalAgentConfig = TechnicalAgentConfig()


class _AgentsFile(_Frozen):
    agents: AgentsConfig


class AppConfig(_Frozen):
    system: SystemConfig
    universe: tuple[UniverseEntry, ...]
    risk: RiskPolicy
    paper_broker: PaperBrokerConfig
    strategies: Mapping[str, StrategyConfig]
    agents: AgentsConfig = AgentsConfig()

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
    agents = (
        _load(config_dir, "agents.yaml", _AgentsFile).agents
        if (config_dir / "agents.yaml").exists()
        else AgentsConfig()
    )
    return AppConfig(
        system=system.system,
        universe=system.universe,
        risk=risk.risk,
        paper_broker=broker.paper_broker,
        strategies=strategies.strategies,
        agents=agents,
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
    llm_api_key_file: Path | None = None  # research agents; absent = agents do not run

    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        auth = quote(self.db_user, safe="")
        if self.db_password_file is not None:
            auth += ":" + quote(read_secret(self.db_password_file), safe="")
        return f"postgresql+psycopg://{auth}@{self.db_host}:{self.db_port}/{self.db_name}"
