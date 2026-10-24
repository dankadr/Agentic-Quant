"""Macro agent: classify the market regime and propose a risk multiplier in [0, 1].

The multiplier can only REDUCE new-entry size (it is never above 1) and it expires: a missing or
stale view blocks new entries of dependent strategies when ``agents.macro.required`` is set. The
agent sees deterministic numeric features only and can do nothing but return this schema.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.llm import LlmError, LlmInvalidOutput, LlmResult
from trading.agents.macro_features import compute_macro_features
from trading.core.config import AppConfig
from trading.core.hashing import canonical_json, content_hash
from trading.core.time import ensure_utc, isoformat_z
from trading.data.calendar import MarketCalendar
from trading.db.models.research import AgentRun
from trading.monitoring.logging import get_logger
from trading.quant.bars import load_session_bars

_log = get_logger(__name__)
AGENT_NAME = "macro"
PROMPT_VERSION = "macro-v1"
INDEX_SYMBOLS = ("SPY", "QQQ", "IWM")
MIN_BARS = 15

SYSTEM_PROMPT = """You are a market regime classifier inside an automated PAPER trading research
system. You receive ONLY numeric features about today's regular session so far. You cannot take
any action; you only answer with one JSON object.

Features: bars (minutes of data), breadth_above_vwap (share of the universe trading above its
session VWAP, 0..1), indices.{SPY,QQQ,IWM}: return_pct (open to now, fraction), realized_vol_bps (std of
1-minute returns in basis points), efficiency (0..1; near 1 = steady one-way move, near 0 = chop),
range_pct (high-low over open).

Regimes: TRENDING (persistent one-way move, high efficiency, broad breadth), RANGING (mean-reverting
chop, low efficiency), HIGH_VOLATILITY (unusually large moves/volatility regardless of direction),
UNKNOWN (features are ambiguous or contradictory).

risk_multiplier is between 0 and 1 and scales DOWN the size of new entries: 1.0 = normal conditions,
lower = be more careful, 0 = no new entries. Never exceed 1. Prefer lower values when uncertain.

Reply with ONLY this JSON object, no other text:
{"regime": "TRENDING|RANGING|HIGH_VOLATILITY|UNKNOWN", "risk_multiplier": 0.0-1.0,
 "confidence": 0.0-1.0, "evidence": ["short reason citing the numbers", "..."],
 "limitations": ["what you could not know"]}"""


class MacroOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    regime: Literal["TRENDING", "RANGING", "HIGH_VOLATILITY", "UNKNOWN"]
    risk_multiplier: Decimal = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=6)
    limitations: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("evidence", "limitations")
    @classmethod
    def _short(cls, items: list[str]) -> list[str]:
        if any(len(i) > 300 for i in items):
            raise ValueError("items must be at most 300 characters")
        return items


class JsonLlm(Protocol):
    def complete_json[T: BaseModel](
        self, system: str, user: str, schema: type[T]
    ) -> LlmResult[T]: ...


@dataclass(frozen=True)
class MacroView:
    run_id: UUID
    regime: str
    risk_multiplier: Decimal
    valid_until: datetime


def latest_macro_view(session: Session, now: datetime) -> MacroView | None:
    """The newest COMPLETED macro run that has not expired at ``now`` (None if there is none)."""
    runs = session.scalars(
        select(AgentRun)
        .where(
            AgentRun.agent_name == AGENT_NAME,
            AgentRun.status == "COMPLETED",
            AgentRun.started_at <= now,
        )
        .order_by(AgentRun.started_at.desc())
        .limit(5)
    ).all()
    for run in runs:
        result = run.result_json or {}
        try:
            valid_until = ensure_utc(datetime.fromisoformat(str(result["valid_until"])))
            multiplier = Decimal(str(result["risk_multiplier"]))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if valid_until > now and Decimal(0) <= multiplier <= Decimal(1):
            return MacroView(run.id, str(result["regime"]), multiplier, valid_until)
        return None  # the newest run is the current view; if it expired there is none
    return None


def run_macro_agent(
    session_factory: sessionmaker[Session],
    llm: JsonLlm,
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    now: datetime,
) -> str:
    """Run once. Returns the stored status; never raises for model or data problems."""
    info = calendar.session_at(now)
    run_id = uuid4()
    model = config.agents.llm.model
    with session_factory.begin() as session:
        session.add(
            AgentRun(
                id=run_id,
                agent_name=AGENT_NAME,
                model=model,
                prompt_version=PROMPT_VERSION,
                started_at=now,
                status="RUNNING",
            )
        )

    def finish(status: str, result: dict[str, object], used_model: str = model) -> str:
        with session_factory.begin() as session:
            row = session.get(AgentRun, run_id)
            assert row is not None  # noqa: S101 - we inserted it above
            row.status = status
            row.completed_at = now
            row.model = used_model
            row.cost_usd = Decimal(0)  # self-hosted endpoint
            row.result_json = json.loads(canonical_json(result))
        return status

    if info is None:
        return finish("FAILED", {"error": "market_closed"})
    universe = [u.symbol for u in config.universe]
    with session_factory() as session:
        bars = load_session_bars(
            session,
            symbols=universe,
            session_date=info.session_date,
            as_of=now,
            provider=provider_name,
        )
    features = compute_macro_features(bars, index_symbols=INDEX_SYMBOLS, min_bars=MIN_BARS)
    if features is None:
        return finish("FAILED", {"error": "insufficient_data"})
    user = json.dumps(features.model_dump(mode="json"), sort_keys=True)
    try:
        result = llm.complete_json(SYSTEM_PROMPT, user, MacroOutput)
    except LlmInvalidOutput as exc:
        _log.warning("macro_agent_invalid_output", error=str(exc))
        return finish("REJECTED_INVALID", {"error": str(exc), "input_hash": content_hash(user)})
    except LlmError as exc:
        _log.warning("macro_agent_llm_failed", error=str(exc))
        return finish("FAILED", {"error": str(exc), "input_hash": content_hash(user)})
    out = result.value
    valid_until = now + timedelta(minutes=config.agents.macro.ttl_minutes)
    return finish(
        "COMPLETED",
        {
            "agent_name": AGENT_NAME,
            "run_id": str(run_id),
            "model_id": result.model,
            "prompt_version": PROMPT_VERSION,
            "created_at": isoformat_z(now),
            "valid_until": isoformat_z(valid_until),
            "regime": out.regime,
            "risk_multiplier": out.risk_multiplier,
            "confidence": out.confidence,
            "evidence": out.evidence,
            "conclusion": f"{out.regime}: new-entry size x{out.risk_multiplier}",
            "limitations": out.limitations,
            "input_hash": content_hash(user),
            "tokens": {"prompt": result.prompt_tokens, "completion": result.completion_tokens},
        },
        used_model=result.model,
    )
