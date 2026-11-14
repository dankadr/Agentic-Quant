"""Versioned, descriptive agent evidence. These scores never authorize an action."""

import re
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.agents.registry import AGENT_REGISTRY, AgentInfo
from trading.core.config import AgentsConfig
from trading.db.models.research import AgentRun

_ET = ZoneInfo("America/New_York")
_TERMINAL = {"COMPLETED", "FAILED", "REJECTED_INVALID"}
_ROLE_QUALITY = {
    "macro": "Regime and confidence calibration require an independent outcome evaluation.",
    "fundamental": "Factual accuracy and citation support require source-level review.",
    "evaluator": (
        "Numerical and narrative correctness require a facts-based review; "
        "legacy v1 is excluded from the current version."
    ),
    "consensus": (
        "Legacy v1 accounting is excluded; current filtered-trade attribution is observational, "
        "not a portfolio replay."
    ),
    "technical": "Proposal validity is distinct from out-of-sample investment performance.",
    "data_quality": "Detection precision and recall require labelled incidents.",
    "execution_quality": (
        "Execution diagnosis accuracy requires labelled incidents and fill evidence."
    ),
    "opportunities": (
        "Activity ranking is not expected profitability; ranking utility is not yet measured."
    ),
}


class RunCounts(BaseModel):
    total: int = 0
    completed: int = 0
    failed: int = 0
    invalid: int = 0
    skipped: int = 0
    inflight: int = 0


class Reliability(BaseModel):
    score: float | None
    numerator: int
    denominator: int
    label: str = "Recorded-run completion (%)"
    on_time_score: float | None = None
    note: str = (
        "Completed / terminal attempts. Skipped and in-progress runs are separate. "
        "Missing scheduled jobs and on-time delivery are not scored."
    )


class Quality(BaseModel):
    output_acceptance_score: float | None
    accepted: int
    evaluated_outputs: int
    factual_accuracy_score: float | None = None
    note: str
    acceptance_note: str = (
        "Schema/domain acceptance proxy only; it does not establish factual correctness. "
        "Infrastructure failures are excluded."
    )


class Contribution(BaseModel):
    score: float | None = None
    status: str = "not_measured"
    paired_sessions: int = 0
    note: str = (
        "No validated agent-specific paired portfolio comparison is available. "
        "Legacy consensus and strategy entry-count experiments do not establish agent value added."
    )


class Observability(BaseModel):
    median_latency_seconds: float | None
    latency_sample: int
    unknown_latency_runs: int
    known_usage_runs: int
    prompt_tokens: int | None
    completion_tokens: int | None
    known_cost_runs: int
    cost_usd: str | None


class PerformanceCohort(BaseModel):
    prompt_version: str
    model: str
    is_current: bool
    observed_days: int
    counts: RunCounts
    reliability: Reliability
    quality: Quality
    contribution: Contribution
    observability: Observability
    error_categories: dict[str, int]


class PerformanceCard(BaseModel):
    name: str
    enabled: bool
    role: str
    current: PerformanceCohort
    cohorts: list[PerformanceCohort]
    legacy_runs: int
    other_model_runs: int


class PerformanceReport(BaseModel):
    as_of: datetime
    since: datetime
    days: int
    items: list[PerformanceCard]
    methodology_version: str = "agent-scorecard-v1"
    note: str = (
        "Descriptive evidence, no overall ranking or automatic changes. "
        "Cohorts separate prompt version and model; configuration matching and statistical "
        "confidence are not inferred. Observed days are calendar dates in Eastern time, "
        "not independent trial counts."
    )


def _percentage(numerator: int, denominator: int) -> float | None:
    return round(100 * numerator / denominator, 4) if denominator else None


def _effective_status(row: AgentRun, now: datetime) -> str:
    if (row.completed_at is not None and row.completed_at > now) or (
        row.available_at is not None and row.available_at > now
    ):
        return "RUNNING"
    return row.status


def _error_category(result: dict[str, Any], status: str) -> str:
    error = str(result.get("error", ""))
    http = re.search(r"HTTP (\d{3})", error)
    if http:
        return "HTTP_" + http.group(1)
    if status == "REJECTED_INVALID":
        return "invalid_output"
    if error in {"stale_input", "insufficient_data", "no_news", "no_experiments"}:
        return error
    return "other_failure"


def _cohort(
    rows: list[AgentRun],
    *,
    name: str,
    info: AgentInfo,
    model: str,
    version: str,
    current: bool,
    now: datetime,
) -> PerformanceCohort:
    statuses = Counter(_effective_status(row, now) for row in rows)
    terminal = sum(statuses[s] for s in _TERMINAL)
    counts = RunCounts(
        total=len(rows),
        completed=statuses["COMPLETED"],
        failed=statuses["FAILED"],
        invalid=statuses["REJECTED_INVALID"],
        skipped=statuses["SKIPPED"],
        inflight=sum(n for s, n in statuses.items() if s not in _TERMINAL and s != "SKIPPED"),
    )
    durations: list[float] = []
    usage: list[tuple[int, int]] = []
    costs: list[Decimal] = []
    errors: Counter[str] = Counter()
    for row in rows:
        result = row.result_json or {}
        status = _effective_status(row, now)
        if status in {"FAILED", "REJECTED_INVALID"}:
            errors[_error_category(result, status)] += 1
        if status not in _TERMINAL:
            continue
        measured = (
            result.get("telemetry_version") == "run-telemetry-v1"
            and result.get("completion_clock") == "runtime"
        )
        legacy_macro = (
            name == "macro" and row.available_at is not None and row.prompt_tokens is not None
        )
        if (measured or legacy_macro) and row.completed_at is not None:
            duration = (row.completed_at - row.started_at).total_seconds()
            if duration >= 0 and (measured or duration > 0):
                durations.append(duration)
        prompt, completion = row.prompt_tokens, row.completion_tokens
        valid_usage = all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (prompt, completion)
        )
        if (
            valid_usage
            and prompt is not None
            and completion is not None
            and result.get("usage_reported") is not False
            and (measured or result.get("usage_reported") is True or (prompt and completion))
        ):
            usage.append((int(prompt), int(completion)))
        if row.cost_usd is not None and (
            not info.uses_llm or result.get("cost_source") == "metered"
        ):
            costs.append(row.cost_usd)
    accepted = counts.completed + counts.invalid
    return PerformanceCohort(
        prompt_version=version,
        model=model,
        is_current=current,
        observed_days=len({r.started_at.astimezone(_ET).date() for r in rows}),
        counts=counts,
        reliability=Reliability(
            score=_percentage(counts.completed, terminal),
            numerator=counts.completed,
            denominator=terminal,
        ),
        quality=Quality(
            output_acceptance_score=_percentage(counts.completed, accepted)
            if info.uses_llm
            else None,
            accepted=counts.completed,
            evaluated_outputs=accepted,
            note=_ROLE_QUALITY[name],
        ),
        contribution=Contribution(),
        observability=Observability(
            median_latency_seconds=round(median(durations), 3) if durations else None,
            latency_sample=len(durations),
            unknown_latency_runs=terminal - len(durations),
            known_usage_runs=len(usage),
            prompt_tokens=sum(p for p, _ in usage) if usage else None,
            completion_tokens=sum(c for _, c in usage) if usage else None,
            known_cost_runs=len(costs),
            cost_usd=str(sum(costs, Decimal(0))) if costs else None,
        ),
        error_categories=dict(errors),
    )


def performance_report(
    session: Session, *, agents: AgentsConfig, since: datetime, now: datetime, days: int
) -> PerformanceReport:
    rows = session.scalars(
        select(AgentRun)
        .where(AgentRun.started_at >= since, AgentRun.started_at <= now)
        .order_by(AgentRun.started_at.desc(), AgentRun.id.desc())
    ).all()
    groups: dict[tuple[str, str, str], list[AgentRun]] = defaultdict(list)
    for row in rows:
        if row.agent_name in AGENT_REGISTRY:
            groups[(row.agent_name, row.prompt_version, row.model)].append(row)
    cards: list[PerformanceCard] = []
    for name, info in AGENT_REGISTRY.items():
        active_model = agents.llm.model if info.uses_llm else "deterministic"
        cohorts = [
            _cohort(
                items,
                name=name,
                info=info,
                model=model,
                version=version,
                current=version == info.prompt_version and model == active_model,
                now=now,
            )
            for (agent, version, model), items in groups.items()
            if agent == name
        ]
        active = next((c for c in cohorts if c.is_current), None) or _cohort(
            [],
            name=name,
            info=info,
            model=active_model,
            version=info.prompt_version,
            current=True,
            now=now,
        )
        cards.append(
            PerformanceCard(
                name=name,
                enabled=getattr(agents, name).enabled,
                role=info.job,
                current=active,
                cohorts=cohorts,
                legacy_runs=sum(
                    c.counts.total for c in cohorts if c.prompt_version != info.prompt_version
                ),
                other_model_runs=sum(
                    c.counts.total
                    for c in cohorts
                    if c.prompt_version == info.prompt_version and c.model != active_model
                ),
            )
        )
    return PerformanceReport(as_of=now, since=since, days=days, items=cards)
