"""Strategies, signals, allocation decisions and agent runs."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from trading.db.base import CASH, Base


class Strategy(Base):
    __tablename__ = "strategies"
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    version: Mapped[str]
    enabled: Mapped[bool]
    capital_budget: Mapped[Decimal] = mapped_column(CASH)
    params_json: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    strategy_version: Mapped[str]
    instrument_id: Mapped[UUID] = mapped_column(ForeignKey("instruments.id"))
    generated_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("market_snapshots.id"))
    side: Mapped[str]
    score: Mapped[Decimal | None]
    target_notional: Mapped[Decimal] = mapped_column(CASH)
    stop_price: Mapped[Decimal | None]
    limit_price: Mapped[Decimal | None]
    reason_codes: Mapped[list[str]] = mapped_column(default=list)
    status: Mapped[str]


class AllocationDecision(Base):
    __tablename__ = "allocation_decisions"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    ts: Mapped[datetime]
    snapshot_id: Mapped[UUID] = mapped_column(ForeignKey("market_snapshots.id"))
    targets_json: Mapped[dict[str, Any]]
    rationale_json: Mapped[dict[str, Any]]


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    agent_name: Mapped[str]
    model: Mapped[str]
    prompt_version: Mapped[str]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    snapshot_id: Mapped[UUID | None] = mapped_column(ForeignKey("market_snapshots.id"))
    result_json: Mapped[dict[str, Any] | None]
    status: Mapped[str]
    cost_usd: Mapped[Decimal | None] = mapped_column(CASH)  # NULL = unknown, never implied zero
    # When the output became usable. NULL on legacy rows: availability is unverifiable, so
    # causal decisions must not use them.
    available_at: Mapped[datetime | None]
    attempt: Mapped[int] = mapped_column(default=1)
    prompt_tokens: Mapped[int | None]
    completion_tokens: Mapped[int | None]


class StrategyConfigProposal(Base):
    __tablename__ = "strategy_config_proposals"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    strategy_id: Mapped[str] = mapped_column(ForeignKey("strategies.id"))
    base_version: Mapped[str]
    parameters_json: Mapped[dict[str, Any]]
    rationale: Mapped[str]
    status: Mapped[str]
    proposed_by: Mapped[str]
    created_at: Mapped[datetime]
    reviewed_by: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    applied_at: Mapped[datetime | None]
    applied_version: Mapped[str | None]


class Experiment(Base):
    __tablename__ = "experiments"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    kind: Mapped[str]
    strategy_id: Mapped[str | None] = mapped_column(ForeignKey("strategies.id"))
    hypothesis: Mapped[str]
    params_json: Mapped[dict[str, Any]]
    data_range_json: Mapped[dict[str, Any]]
    metrics_json: Mapped[dict[str, Any]]
    status: Mapped[str]
    notes: Mapped[str]
    created_at: Mapped[datetime]
