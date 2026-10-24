"""Evaluator agent: a daily narrative over the end-of-day report.

Analysis only. It cannot place orders, change parameters or create proposals; its output is stored
in ``agent_runs`` for a human to read.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.llm import LlmError, LlmInvalidOutput
from trading.agents.macro import JsonLlm
from trading.core.config import AppConfig
from trading.core.hashing import canonical_json, content_hash
from trading.core.time import isoformat_z
from trading.db.models.research import AgentRun
from trading.monitoring.logging import get_logger
from trading.monitoring.report import daily_report

_log = get_logger(__name__)
AGENT_NAME = "evaluator"
PROMPT_VERSION = "evaluator-v1"

SYSTEM_PROMPT = """You review one day of an automated PAPER trading system (ORB and VWAP-reversion
strategies on liquid ETFs/stocks, IEX data, simulated fills). You receive a JSON end-of-day report:
data coverage per symbol (missing/flagged bars), strategy signal counts and P&L attribution, orders,
risk denials, costs, equity change and the SPY benchmark.

Write an honest, plain assessment. Say when the sample is too small to conclude anything. Point out
data problems first (missing or flagged bars, zero ticks), then what each strategy did, then costs
versus gross result. Do not invent numbers that are not in the report. You cannot act on anything;
suggestions are for a human to consider.

Reply with ONLY this JSON object:
{"summary": "<=1200 chars", "findings": ["..."], "suggestions": ["..."]}"""


class EvaluatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    summary: str = Field(min_length=1, max_length=1200)
    findings: list[str] = Field(default_factory=list, max_length=10)
    suggestions: list[str] = Field(default_factory=list, max_length=6)


def run_evaluator_agent(
    session_factory: sessionmaker[Session],
    llm: JsonLlm,
    *,
    config: AppConfig,
    session_date: date,
    provider_name: str,
    now: datetime,
) -> str:
    run_id: UUID = uuid4()
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
            assert row is not None  # noqa: S101 - inserted above
            row.status = status
            row.completed_at = now
            row.model = used_model
            row.cost_usd = Decimal(0)
            row.result_json = json.loads(canonical_json(result))
        return status

    with session_factory() as session:
        report = daily_report(session, session_date, config=config, provider=provider_name)
    user = canonical_json(report)
    digest = content_hash(user)
    try:
        result = llm.complete_json(SYSTEM_PROMPT, user, EvaluatorOutput)
    except LlmInvalidOutput as exc:
        return finish("REJECTED_INVALID", {"error": str(exc), "input_hash": digest})
    except LlmError as exc:
        _log.warning("evaluator_llm_failed", error=str(exc))
        return finish("FAILED", {"error": str(exc), "input_hash": digest})
    out = result.value
    return finish(
        "COMPLETED",
        {
            "agent_name": AGENT_NAME,
            "run_id": str(run_id),
            "model_id": result.model,
            "prompt_version": PROMPT_VERSION,
            "created_at": isoformat_z(now),
            "session_date": session_date.isoformat(),
            "summary": out.summary,
            "findings": out.findings,
            "suggestions": out.suggestions,
            "input_hash": digest,
        },
        used_model=result.model,
    )
