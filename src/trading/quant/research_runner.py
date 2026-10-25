"""Run walk-forward and sensitivity research and record every hypothesis in the registry.

Each distinct parameter set is backtested ONCE over the whole date range (``FullRun``); windows are
then sliced out of that single run. That is far cheaper than a fresh backtest per window and just
as deterministic. (Sizing uses equity, which compounds across the range, so a window's numbers can
differ marginally from a standalone backtest of that window.)

Backtests need a scratch database; experiments are written to the *registry* database, which is
normally the live one. The final holdout is evaluated at most once per strategy and date range.
"""

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.errors import TradingError
from trading.core.hashing import canonical_json
from trading.quant.research import (
    Evaluation,
    Evaluator,
    SensitivityResult,
    WalkForwardResult,
    record_experiment,
    sensitivity,
    walk_forward,
)

FullRun = Callable[[Mapping[str, Any]], Mapping[date, Decimal]]
_ZERO = Decimal(0)
_STATUS = {"SUPPORTED": "COMPLETED", "REJECTED": "REJECTED", "INCONCLUSIVE": "INCONCLUSIVE"}


class HoldoutAlreadyUsed(TradingError):
    """The protected holdout for this strategy and range was already evaluated once."""


def _drawdown(returns: Sequence[Decimal]) -> Decimal:
    equity = peak = Decimal(1)
    worst = _ZERO
    for r in returns:
        equity *= 1 + r
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def disk_cached(full_run: FullRun, directory: Path, fingerprint: Mapping[str, Any]) -> FullRun:
    """Persist each parameter set's full run so a long research job can resume after a restart.

    The key covers the parameters AND ``fingerprint`` (strategy, data files, dates, ...): changed
    data or code-relevant inputs never reuse a stale result. Writes are atomic.
    """
    directory.mkdir(parents=True, exist_ok=True)

    def run(params: Mapping[str, Any]) -> Mapping[date, Decimal]:
        key = hashlib.sha256(
            canonical_json({"fp": dict(fingerprint), "params": dict(params)}).encode()
        ).hexdigest()[:24]
        path = directory / f"{key}.json"
        if path.exists():
            stored = json.loads(path.read_text())
            return {date.fromisoformat(d): Decimal(v) for d, v in stored.items()}
        result = dict(full_run(params))
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({d.isoformat(): str(v) for d, v in result.items()}))
        os.replace(tmp, path)
        return result

    return run


def cached_evaluator(full_run: FullRun, *, base_equity: Decimal) -> Evaluator:
    cache: dict[str, Mapping[date, Decimal]] = {}

    def evaluate(dates: Sequence[date], params: Mapping[str, Any]) -> Evaluation:
        key = canonical_json(dict(params))
        if key not in cache:
            cache[key] = full_run(params)
        by_day = cache[key]
        returns = tuple(by_day.get(d, _ZERO) for d in dates)
        return Evaluation(
            params=dict(params),
            dates=tuple(dates),
            net_pnl=sum(returns, _ZERO) * base_equity,
            daily_returns=returns,
            trades=0,
            expectancy=_ZERO,
            max_drawdown_pct=_drawdown(returns),
        )

    return evaluate


@dataclass(frozen=True)
class ResearchOutcome:
    walk_forward: WalkForwardResult
    sensitivity: SensitivityResult
    holdout: Evaluation | None


def _holdout_used(session: Session, strategy_id: str, holdout: Sequence[str]) -> bool:
    rows = session.execute(
        text(
            "SELECT data_range_json->'holdout' AS h FROM experiments"
            " WHERE strategy_id = :s AND notes LIKE '%holdout evaluation%'"
        ),
        {"s": strategy_id},
    ).all()
    return any(list(r.h or []) == list(holdout) for r in rows)


def run_research(
    registry: sessionmaker[Session],
    full_run: FullRun,
    *,
    strategy_id: str,
    dates: Sequence[date],
    base_params: Mapping[str, Any],
    space: Mapping[str, Sequence[Any]],
    neighbors: Mapping[str, Sequence[Any]],
    train: int,
    test: int,
    holdout: int,
    base_equity: Decimal,
    now: datetime,
    evaluate_holdout: bool = False,
    strategy_version: str | None = None,
) -> ResearchOutcome:
    ordered = sorted(dates)
    held = [d.isoformat() for d in ordered[max(0, len(ordered) - holdout) :]] if holdout else []
    if evaluate_holdout:
        if not held:
            raise ValueError("no holdout configured")
        with registry() as session:
            if _holdout_used(session, strategy_id, held):
                raise HoldoutAlreadyUsed(
                    f"{strategy_id}: holdout {held[0]}..{held[-1]} already used"
                )
    evaluator = cached_evaluator(full_run, base_equity=base_equity)
    wf = walk_forward(
        evaluator, ordered, base_params, space, train=train, test=test, holdout=holdout
    )
    searched = ordered[: max(0, len(ordered) - holdout)]
    sens = sensitivity(evaluator, searched, base_params, neighbors)
    data_range = {
        "first": ordered[0].isoformat(),
        "last": ordered[-1].isoformat(),
        "days": len(ordered),
        "holdout": held,
    }
    chosen = [f.chosen for f in wf.folds]
    held_eval: Evaluation | None = None
    if evaluate_holdout and wf.folds:
        modal, _ = Counter(canonical_json(c) for c in chosen).most_common(1)[0]
        params = next(c for c in chosen if canonical_json(c) == modal)
        held_eval = evaluator(wf.holdout, params)
    with registry.begin() as session:
        record_experiment(
            session,
            kind="WALK_FORWARD",
            strategy_id=strategy_id,
            hypothesis=f"{strategy_id}: parameters chosen on train windows beat zero out of sample",
            params={
                "base": dict(base_params),
                "space": {k: list(v) for k, v in space.items()},
                "train": train,
                "test": test,
                "strategy_version": strategy_version,
            },
            data_range=data_range,
            metrics={
                "verdict": wf.verdict,
                "chosen": chosen,
                "in_sample_net_pnl": wf.in_sample_net_pnl,
                "out_of_sample_net_pnl": wf.out_of_sample_net_pnl,
                "degradation": wf.degradation,
                "p_value": wf.out_of_sample_p_value,
                "hypotheses_tested": wf.hypotheses_tested,
                "folds": len(wf.folds),
            },
            status=_STATUS[wf.verdict],
            now=now,
        )
        record_experiment(
            session,
            kind="SENSITIVITY",
            strategy_id=strategy_id,
            hypothesis=f"{strategy_id}: the base parameters survive one-at-a-time perturbation",
            params={
                "base": dict(base_params),
                "neighbors": {k: list(v) for k, v in neighbors.items()},
                "strategy_version": strategy_version,
            },
            data_range=data_range,
            metrics={
                "robust": sens.robust,
                "fraction_positive": sens.fraction_positive,
                "base_net_pnl": sens.base.net_pnl,
                "neighbor_net_pnl": [n.net_pnl for n in sens.neighbors],
            },
            status="COMPLETED" if sens.robust else "REJECTED",
            now=now,
        )
        if held_eval is not None:
            record_experiment(
                session,
                kind="BACKTEST",
                strategy_id=strategy_id,
                hypothesis=f"{strategy_id}: the selected parameters hold on the final data",
                params={"chosen": held_eval.params},
                data_range=data_range,
                metrics={
                    "net_pnl": held_eval.net_pnl,
                    "max_drawdown_pct": held_eval.max_drawdown_pct,
                },
                status="COMPLETED" if held_eval.net_pnl > 0 else "REJECTED",
                notes="final holdout evaluation (single use)",
                now=now,
            )
    return ResearchOutcome(wf, sens, held_eval)
