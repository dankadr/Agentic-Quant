"""Evaluator agent: a daily narrative over the end-of-day report.

Analysis only. It cannot place orders, change parameters or create proposals; its output is stored
in ``agent_runs`` for a human to read.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
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
PROMPT_VERSION = "evaluator-v3"

ExplanationCode = Literal[
    "SMALL_SAMPLE",
    "CHECK_DATA",
    "INCLUDED_COSTS",
    "REALIZED_ONLY",
    "RECONCILIATION",
    "BENCHMARK_CONTEXT",
    "OBSERVATION",
]
SuggestionCode = Literal[
    "INVESTIGATE_DATA",
    "REVIEW_EXECUTION",
    "MORE_HISTORY",
    "REVIEW_RECONCILIATION",
    "REVIEW_STRATEGY",
]
EXPLANATIONS: dict[str, str] = {
    "SMALL_SAMPLE": "A single session cannot establish an edge.",
    "CHECK_DATA": "Check completeness and freshness before interpreting the result.",
    "INCLUDED_COSTS": "Execution costs already affect recorded P&L.",
    "REALIZED_ONLY": "Realized attribution is not the full change in account equity.",
    "RECONCILIATION": "Investigate any unexplained accounting residual.",
    "BENCHMARK_CONTEXT": "The benchmark supplies context, not evidence of a durable edge.",
    "OBSERVATION": "Review this recorded metric alongside the rest of the session.",
}
SUGGESTIONS: dict[str, str] = {
    "INVESTIGATE_DATA": "Investigate data completeness and freshness.",
    "REVIEW_EXECUTION": "Investigate execution quality.",
    "MORE_HISTORY": "Collect more independent sessions before drawing conclusions.",
    "REVIEW_RECONCILIATION": "Review accounting and attribution reconciliation.",
    "REVIEW_STRATEGY": "Review strategy evidence before proposing parameter changes.",
}
EXPLANATION_SCOPES = {
    "CHECK_DATA": "data.",
    "INCLUDED_COSTS": "costs.",
    "REALIZED_ONLY": "strategy.",
    "RECONCILIATION": "equity.reconciliation",
    "BENCHMARK_CONTEXT": "benchmark.return",
}

SYSTEM_PROMPT = """Review an automated PAPER trading session. Facts are indexed by metric id.
Select findings and suggestions using only the codes below; no free text is accepted or displayed.
All displayed explanations and numeric values are rendered by deterministic code.
Explanation codes: SMALL_SAMPLE, CHECK_DATA (data metrics), INCLUDED_COSTS (cost metrics),
REALIZED_ONLY (strategy metrics), RECONCILIATION (equity.reconciliation),
BENCHMARK_CONTEXT (benchmark.return), OBSERVATION.
Suggestion codes: INVESTIGATE_DATA, REVIEW_EXECUTION, MORE_HISTORY,
REVIEW_RECONCILIATION, REVIEW_STRATEGY.
Return ONLY JSON:
{"findings": [{"metric_id": "an id from facts", "explanation_code": "SMALL_SAMPLE"}],
 "suggestions": ["MORE_HISTORY"]}.
You cannot act, change settings, or place orders.
"""


class MetricFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    metric_id: str = Field(min_length=1, max_length=120)
    explanation_code: ExplanationCode


class EvaluatorOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    findings: list[MetricFinding] = Field(default_factory=list, max_length=10)
    suggestions: list[SuggestionCode] = Field(default_factory=list, max_length=6)


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
        scope = EXPLANATION_SCOPES.get(finding.explanation_code)
        if scope and not finding.metric_id.startswith(scope):
            raise ValueError("explanation code does not apply to metric")
        findings.append(f"{facts[finding.metric_id]} {EXPLANATIONS[finding.explanation_code]}")
    summary = (
        f"Paper session {report.get('date')}. {facts.get('equity.pnl', facts['session.ticks'])} "
        "A single session cannot establish an edge."
    )
    return {
        "summary": summary,
        "findings": findings,
        "suggestions": [SUGGESTIONS[code] for code in out.suggestions],
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
