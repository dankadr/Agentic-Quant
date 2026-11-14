"""Evaluator agent: a daily narrative over the end-of-day report.

Analysis only. It cannot place orders, change parameters or create proposals; its output is stored
in ``agent_runs`` for a human to read.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
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
PROMPT_VERSION = "evaluator-v2"

SYSTEM_PROMPT = """Review an automated PAPER trading session. The report includes deterministic
facts indexed by metric id. Only explain those facts qualitatively; do not compute or repeat any
numeric values, amounts, percentages, dates or counts in your text. Numbers are rendered by code.
One session is insufficient to establish profitability. Slippage estimates already affect fill
prices and must not be subtracted again. Attribution is realized P&L, not total equity change.
Return ONLY JSON:
{"findings": [{"metric_id": "an id from facts", "explanation": "qualitative explanation"}],
 "suggestions": ["qualitative suggestion without numbers"]}.
You cannot act, change settings, or place orders. No digits or percent signs in explanations.
"""


class MetricFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric_id: str = Field(min_length=1, max_length=120)
    explanation: str = Field(min_length=1, max_length=500)

    @field_validator("explanation")
    @classmethod
    def no_numeric_claims(cls, value: str) -> str:
        if any(c.isnumeric() for c in value) or "%" in value or "$" in value:
            raise ValueError("numeric claims must come from deterministic facts")
        return value


class EvaluatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    findings: list[MetricFinding] = Field(default_factory=list, max_length=10)
    suggestions: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("suggestions")
    @classmethod
    def no_numeric_suggestions(cls, values: list[str]) -> list[str]:
        for value in values:
            MetricFinding.no_numeric_claims(value)
            if len(value) > 500:
                raise ValueError("suggestion too long")
        return values


def report_facts(report: dict[str, object]) -> dict[str, str]:
    """Only this code formats numbers shown to an owner; model prose is separate."""
    facts = {"session.ticks": f"Recorded ticks: {report.get('ticks', 0)}."}
    equity = report.get("equity")
    if isinstance(equity, dict):
        facts["equity.pnl"] = (
            f"Trading P&L: ${equity.get('pnl')}; return: {equity.get('return_pct_display')}."
        )
        facts["equity.drawdown"] = f"Maximum drawdown: {equity.get('max_drawdown_pct_display')}."
        facts["equity.reconciliation"] = (
            f"Equity reconciliation residual: ${equity.get('reconciliation_residual')}; "
            f"attribution residual: ${equity.get('attribution_residual')}."
        )
    benchmark = report.get("benchmark")
    if isinstance(benchmark, dict):
        facts["benchmark.return"] = (
            f"{benchmark.get('symbol', 'SPY')} return: {benchmark.get('return_pct_display')}."
        )
    costs = report.get("costs")
    if isinstance(costs, dict):
        facts["costs.fees"] = f"Fees: ${costs.get('fees')}, included in P&L."
        facts["costs.slippage"] = (
            f"Slippage estimate: ${costs.get('slippage_estimate')}, already reflected "
            "in fill prices; do not subtract again."
        )
    coverage_rows = report.get("data_coverage")
    if isinstance(coverage_rows, dict):
        for symbol, coverage in coverage_rows.items():
            facts[f"data.{symbol}"] = f"{symbol} data coverage: {coverage}."
    strategies = report.get("strategies")
    if isinstance(strategies, dict):
        for strategy, values in strategies.items():
            facts[f"strategy.{strategy}"] = (
                f"{strategy} realized attributed P&L: ${values.get('net_pnl')}; "
                f"signals: {values.get('signals')}."
            )
    return facts


def render_review(report: dict[str, object], out: EvaluatorOutput) -> dict[str, object]:
    facts = report_facts(report)
    findings = []
    for finding in out.findings:
        if finding.metric_id not in facts:
            raise ValueError(f"unknown metric {finding.metric_id!r}")
        findings.append(f"{facts[finding.metric_id]} {finding.explanation}")
    summary = (
        f"Paper session {report.get('date')}. {facts.get('equity.pnl', facts['session.ticks'])} "
        "A single session cannot establish an edge."
    )
    return {
        "summary": summary,
        "findings": findings,
        "suggestions": out.suggestions,
        "facts": facts,
    }


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
    user = canonical_json({**report, "facts": report_facts(report)})
    digest = content_hash(user)
    try:
        result = llm.complete_json(SYSTEM_PROMPT, user, EvaluatorOutput)
    except LlmInvalidOutput as exc:
        return finish("REJECTED_INVALID", {"error": str(exc), "input_hash": digest})
    except LlmError as exc:
        _log.warning("evaluator_llm_failed", error=str(exc))
        return finish("FAILED", {"error": str(exc), "input_hash": digest})
    out = result.value
    try:
        rendered = render_review(report, out)
    except ValueError as exc:
        return finish("REJECTED_INVALID", {"error": str(exc), "input_hash": digest})
    return finish(
        "COMPLETED",
        {
            "agent_name": AGENT_NAME,
            "run_id": str(run_id),
            "model_id": result.model,
            "prompt_version": PROMPT_VERSION,
            "created_at": isoformat_z(now),
            "session_date": session_date.isoformat(),
            **rendered,
            "metric_findings": [f.model_dump() for f in out.findings],
            "input_hash": digest,
        },
        used_model=result.model,
    )
