"""Deterministic, evidence-gated suggestions for agent settings, from recorded runs and trades.

No model is involved. Every suggestion carries how much evidence stands behind it; below the
configured thresholds it is only "watching" and cannot be turned into a proposal. Suggestions
that change nothing (``proposed is None``) are informational and never proposable.
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.config import AgentsConfig
from trading.db.models.portfolio import StrategyAttribution
from trading.db.models.research import AgentRun
from trading.db.models.trading import FillRow

LOOKBACK = timedelta(days=60)
FLIP_RATE_LIMIT = 0.5
UNKNOWN_SHARE_LIMIT = 0.25
INTERVAL_STEP = 15


@dataclass(frozen=True)
class Suggestion:
    agent: str
    title: str
    field: str | None
    current: Any
    proposed: Any | None
    evidence_n: int
    evidence_needed: int
    status: Literal["watching", "proposable"]
    rationale: str


def pnl_by_view_class(
    views: Sequence[tuple[datetime, str]], fills: Sequence[tuple[datetime, Decimal]]
) -> dict[str, tuple[int, Decimal]]:
    """Realised P&L grouped by the macro view that was the latest one when it was booked."""
    ordered = sorted(views)
    times = [t for t, _ in ordered]
    out: dict[str, tuple[int, Decimal]] = {}
    for ts, pnl in fills:
        i = bisect_right(times, ts) - 1
        if i < 0:
            continue
        key = "UNKNOWN" if ordered[i][1] == "UNKNOWN" else "OTHER"
        n, total = out.get(key, (0, Decimal(0)))
        out[key] = (n + 1, total + pnl)
    return out


def _regime(run: AgentRun) -> str | None:
    value = run.result_json.get("regime") if isinstance(run.result_json, dict) else None
    return value if isinstance(value, str) else None


def _status(
    config: AgentsConfig, n: int, days: int, proposed: object
) -> Literal["watching", "proposable"]:
    enough = n >= config.suggestions.min_decisions and days >= config.suggestions.min_days
    return "proposable" if enough and proposed is not None else "watching"


def _macro(session: Session, config: AgentsConfig, now: datetime) -> list[Suggestion]:
    runs = list(
        session.scalars(
            select(AgentRun)
            .where(
                AgentRun.agent_name == "macro",
                AgentRun.status == "COMPLETED",
                AgentRun.started_at >= now - LOOKBACK,
            )
            .order_by(AgentRun.started_at)
        )
    )
    regimes = [(r.started_at, g) for r in runs if (g := _regime(r)) is not None]
    n = len(regimes)
    days = len({t.date() for t, _ in regimes})
    out: list[Suggestion] = []
    needed = config.suggestions.min_decisions

    if n >= 2:
        flips = sum(a[1] != b[1] for a, b in pairwise(regimes))
        rate = flips / (n - 1)
        if rate > FLIP_RATE_LIMIT:
            current = config.macro.run_every_minutes
            proposed: int | None = current + INTERVAL_STEP
            try:
                AgentsConfig.model_validate(
                    {
                        **config.model_dump(),
                        "macro": {**config.macro.model_dump(), "run_every_minutes": proposed},
                    }
                )
            except ValidationError:
                proposed = None  # would break a rule (e.g. ttl); keep it visible but not proposable
            out.append(
                Suggestion(
                    agent="macro",
                    title="Run the macro agent less often?",
                    field="run_every_minutes",
                    current=current,
                    proposed=proposed,
                    evidence_n=n,
                    evidence_needed=needed,
                    status=_status(config, n, days, proposed),
                    rationale=f"The regime changed between {rate:.0%} of consecutive runs "
                    f"({flips} of {n - 1}); frequent flips make the size multiplier jumpy.",
                )
            )

    unknown = sum(g == "UNKNOWN" for _, g in regimes)
    if n >= 3 and unknown / n > UNKNOWN_SHARE_LIMIT:
        fills = [
            (ts, pnl)
            for ts, pnl in session.execute(
                select(FillRow.ts, StrategyAttribution.pnl)
                .join(StrategyAttribution, StrategyAttribution.fill_id == FillRow.id)
                .where(StrategyAttribution.pnl.is_not(None), FillRow.ts >= now - LOOKBACK)
            )
            if pnl is not None
        ]
        grouped = pnl_by_view_class(regimes, fills)
        detail = "; ".join(
            f"realised P&L booked while a {k} view was latest: {total} over {count} fills"
            for k, (count, total) in sorted(grouped.items())
        )
        out.append(
            Suggestion(
                agent="macro",
                title=f"UNKNOWN is {unknown / n:.0%} of macro views",
                field=None,
                current=None,
                proposed=None,
                evidence_n=n,
                evidence_needed=needed,
                status="watching",
                rationale="Mostly a data-window effect early in the session, so no setting is "
                "proposed. " + (detail or "No attributed P&L recorded yet."),
            )
        )
    return out


def _evaluator(session: Session, now: datetime) -> list[Suggestion]:
    runs = list(
        session.scalars(
            select(AgentRun)
            .where(
                AgentRun.agent_name == "evaluator",
                AgentRun.status.in_(("REJECTED_INVALID", "FAILED")),
                AgentRun.started_at >= now - LOOKBACK,
            )
            .order_by(AgentRun.started_at.desc())
        )
    )
    if not runs:
        return []
    error = runs[0].result_json.get("error") if isinstance(runs[0].result_json, dict) else None
    return [
        Suggestion(
            agent="evaluator",
            title="Evaluator replies are being rejected",
            field=None,
            current=None,
            proposed=None,
            evidence_n=len(runs),
            evidence_needed=1,
            status="watching",
            rationale=str(error) if error else "The latest evaluator run failed.",
        )
    ]


def suggest(
    session: Session, name: str, *, config: AgentsConfig, now: datetime
) -> list[Suggestion]:
    if name == "macro":
        return _macro(session, config, now)
    if name == "evaluator":
        return _evaluator(session, now)
    return []
