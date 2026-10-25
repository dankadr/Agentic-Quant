"""Research tooling: parameter grids, walk-forward validation, sensitivity, experiment registry.

The evaluator is injected (a backtest in production, a fake in unit tests). Parameters are chosen
on TRAIN windows only and scored once on the following TEST window; the holdout is never touched.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy.orm import Session

from trading.core.hashing import canonical_json
from trading.db.models.research import Experiment
from trading.quant.stats import holm_bonferroni, mean_return_p_value

D = Decimal
ALPHA = D("0.05")
MIN_FRACTION_POSITIVE = D("0.7")
Verdict = Literal["SUPPORTED", "REJECTED", "INCONCLUSIVE"]


@dataclass(frozen=True)
class Evaluation:
    params: dict[str, Any]
    dates: tuple[date, ...]
    net_pnl: Decimal
    daily_returns: tuple[Decimal, ...]
    trades: int
    expectancy: Decimal
    max_drawdown_pct: Decimal


Evaluator = Callable[[Sequence[date], Mapping[str, Any]], Evaluation]


@dataclass(frozen=True)
class Fold:
    train: tuple[date, ...]
    test: tuple[date, ...]


@dataclass(frozen=True)
class FoldResult:
    fold: Fold
    chosen: dict[str, Any]
    train: Evaluation
    test: Evaluation


@dataclass(frozen=True)
class WalkForwardResult:
    folds: list[FoldResult]
    in_sample_net_pnl: Decimal
    out_of_sample_net_pnl: Decimal
    out_of_sample_returns: tuple[Decimal, ...]
    out_of_sample_p_value: Decimal | None
    degradation: Decimal | None
    hypotheses_tested: int
    hypothesis_rejections: list[bool]
    candidates: int
    verdict: Verdict
    holdout: tuple[date, ...] = field(default=())


def parameter_grid(space: Mapping[str, Sequence[Any]]) -> list[dict[str, Any]]:
    keys = sorted(space)
    return [
        dict(zip(keys, combo, strict=True))
        for combo in itertools.product(*(space[k] for k in keys))
    ]


def make_folds(
    dates: Sequence[date], *, train: int, test: int, step: int | None = None, holdout: int = 0
) -> tuple[list[Fold], tuple[date, ...]]:
    step = test if step is None else step
    if train < 1 or test < 1 or step < 1 or holdout < 0:
        raise ValueError("train, test and step must be >= 1 and holdout >= 0")
    ordered = sorted(dates)
    usable = ordered[: len(ordered) - holdout]
    held = tuple(ordered[len(ordered) - holdout :]) if holdout else ()
    folds: list[Fold] = []
    start = 0
    while start + train + test <= len(usable):
        folds.append(
            Fold(
                tuple(usable[start : start + train]),
                tuple(usable[start + train : start + train + test]),
            )
        )
        start += step
    return folds, held


def walk_forward(
    evaluator: Evaluator,
    dates: Sequence[date],
    base_params: Mapping[str, Any],
    space: Mapping[str, Sequence[Any]],
    *,
    train: int,
    test: int,
    step: int | None = None,
    holdout: int = 0,
    alpha: Decimal = ALPHA,
) -> WalkForwardResult:
    folds, held = make_folds(dates, train=train, test=test, step=step, holdout=holdout)
    grid = parameter_grid(space)
    results: list[FoldResult] = []
    train_p_values: list[Decimal] = []
    for fold in folds:
        best: Evaluation | None = None
        for candidate in grid:
            ev = evaluator(fold.train, {**base_params, **candidate})
            p = mean_return_p_value(ev.daily_returns)
            train_p_values.append(D(1) if p is None else p)
            if best is None or ev.net_pnl > best.net_pnl:  # strict: ties keep the earlier candidate
                best = ev
        if best is None:
            raise ValueError("parameter space is empty")
        scored = evaluator(fold.test, best.params)
        results.append(FoldResult(fold, dict(best.params), best, scored))

    is_pnl = sum((r.train.net_pnl for r in results), D(0))
    oos_pnl = sum((r.test.net_pnl for r in results), D(0))
    oos_returns = tuple(x for r in results for x in r.test.daily_returns)
    is_days = sum(len(r.fold.train) for r in results)
    oos_days = sum(len(r.fold.test) for r in results)
    degradation: Decimal | None = None
    if results and is_pnl > 0:
        degradation = (oos_pnl / oos_days) / (is_pnl / is_days)
    p_value = mean_return_p_value(oos_returns)
    rejections = holm_bonferroni(train_p_values, alpha=alpha) if train_p_values else []

    verdict: Verdict
    if not results:
        verdict = "INCONCLUSIVE"
    elif oos_pnl <= 0:
        verdict = "REJECTED"
    elif p_value is not None and p_value * len(grid) < alpha:
        verdict = "SUPPORTED"
    else:
        verdict = "INCONCLUSIVE"
    return WalkForwardResult(
        folds=results,
        in_sample_net_pnl=is_pnl,
        out_of_sample_net_pnl=oos_pnl,
        out_of_sample_returns=oos_returns,
        out_of_sample_p_value=p_value,
        degradation=degradation,
        hypotheses_tested=len(train_p_values),
        hypothesis_rejections=rejections,
        candidates=len(grid),
        verdict=verdict,
        holdout=held,
    )


@dataclass(frozen=True)
class SensitivityResult:
    base: Evaluation
    neighbors: list[Evaluation]
    fraction_positive: Decimal
    robust: bool


def sensitivity(
    evaluator: Evaluator,
    dates: Sequence[date],
    base_params: Mapping[str, Any],
    neighbors: Mapping[str, Sequence[Any]],
    *,
    min_fraction_positive: Decimal = MIN_FRACTION_POSITIVE,
) -> SensitivityResult:
    """Perturb one parameter at a time; a knife-edge optimum has losing neighbours."""
    base = evaluator(dates, dict(base_params))
    around = [
        evaluator(dates, {**base_params, key: value})
        for key in sorted(neighbors)
        for value in neighbors[key]
    ]
    positive = sum(1 for ev in around if ev.net_pnl > 0)
    fraction = D(positive) / D(len(around)) if around else D(0)
    return SensitivityResult(
        base=base,
        neighbors=around,
        fraction_positive=fraction,
        robust=base.net_pnl > 0 and fraction >= min_fraction_positive,
    )


def record_experiment(
    session: Session,
    *,
    kind: str,
    hypothesis: str,
    params: Mapping[str, Any],
    data_range: Mapping[str, Any],
    metrics: Mapping[str, Any],
    status: str,
    strategy_id: str | None = None,
    notes: str = "",
    now: Any,
) -> Experiment:
    """Every hypothesis is recorded, including rejected ones, so selection bias stays visible."""
    row = Experiment(
        id=uuid4(),
        kind=kind,
        strategy_id=strategy_id,
        hypothesis=hypothesis,
        params_json=json.loads(canonical_json(dict(params))),
        data_range_json=json.loads(canonical_json(dict(data_range))),
        metrics_json=json.loads(canonical_json(dict(metrics))),
        status=status,
        notes=notes,
        created_at=now,
    )
    session.add(row)
    session.flush()
    return row
