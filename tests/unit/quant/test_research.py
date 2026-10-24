from datetime import date, timedelta
from decimal import Decimal

import pytest

from trading.quant.research import (
    Evaluation,
    make_folds,
    parameter_grid,
    sensitivity,
    walk_forward,
)

D = Decimal
DAYS = [date(2026, 10, 1) + timedelta(days=i) for i in range(10)]


def test_parameter_grid_is_the_deterministic_cartesian_product() -> None:
    grid = parameter_grid({"b": [1, 2], "a": ["x", "y", "z"]})
    assert len(grid) == 6
    assert grid[0] == {"a": "x", "b": 1}
    assert grid[-1] == {"a": "z", "b": 2}
    assert grid == parameter_grid({"a": ["x", "y", "z"], "b": [1, 2]})  # key order is irrelevant
    assert parameter_grid({}) == [{}]


def test_folds_roll_forward_without_leaking_test_into_train() -> None:
    folds, holdout = make_folds(DAYS, train=4, test=2, step=2, holdout=0)
    assert holdout == ()
    assert [(f.train[0], f.train[-1], f.test[0], f.test[-1]) for f in folds] == [
        (DAYS[0], DAYS[3], DAYS[4], DAYS[5]),
        (DAYS[2], DAYS[5], DAYS[6], DAYS[7]),
        (DAYS[4], DAYS[7], DAYS[8], DAYS[9]),
    ]
    for f in folds:
        assert max(f.train) < min(f.test)
        assert not set(f.train) & set(f.test)


def test_the_holdout_is_never_part_of_any_fold() -> None:
    folds, holdout = make_folds(DAYS, train=4, test=2, step=2, holdout=2)
    assert holdout == (DAYS[8], DAYS[9])
    assert all(d not in holdout for f in folds for d in (*f.train, *f.test))
    assert folds[-1].test[-1] == DAYS[7]


def test_too_little_data_gives_no_folds() -> None:
    folds, _ = make_folds(DAYS[:3], train=4, test=2)
    assert folds == []


def test_invalid_fold_arguments_are_rejected() -> None:
    for kwargs in (
        {"train": 0, "test": 1},
        {"train": 1, "test": 0},
        {"train": 2, "test": 1, "step": 0},
        {"train": 2, "test": 1, "holdout": -1},
    ):
        with pytest.raises(ValueError):
            make_folds(DAYS, **kwargs)


class FakeEvaluator:
    """pnl = (quality of params) per day, so the best candidate is knowable."""

    def __init__(self, quality: dict[int, Decimal], noise: Decimal | None = None) -> None:
        self.quality = quality
        self.calls: list[tuple[tuple[date, ...], int]] = []
        self.noise = noise or D(0)

    def __call__(self, dates, params):  # type: ignore[no-untyped-def]
        self.calls.append((tuple(dates), params["x"]))
        per_day = self.quality[params["x"]]
        returns = tuple(
            per_day / 1000 + (self.noise if i % 2 else -self.noise) for i, _ in enumerate(dates)
        )
        return Evaluation(params=dict(params), dates=tuple(dates), net_pnl=per_day * len(dates), daily_returns=returns,
                          trades=len(dates), expectancy=per_day, max_drawdown_pct=D(0))  # fmt: skip


def test_walk_forward_selects_on_train_and_scores_on_test() -> None:
    ev = FakeEvaluator({1: D("1"), 2: D("3"), 3: D("2")})
    result = walk_forward(ev, DAYS, {"x": 1}, {"x": [1, 2, 3]}, train=4, test=2, step=2, holdout=0)
    assert [f.chosen["x"] for f in result.folds] == [2, 2, 2]
    # every test window is evaluated exactly once, and only with the chosen parameters
    test_calls = [c for c in ev.calls if len(c[0]) == 2]
    assert len(test_calls) == 3
    assert {x for _, x in test_calls} == {2}
    train_calls = [c for c in ev.calls if len(c[0]) == 4]
    assert len(train_calls) == 9  # 3 folds x 3 candidates
    assert result.out_of_sample_net_pnl == D("3") * 6
    assert len(result.out_of_sample_returns) == 6
    assert result.hypotheses_tested == 9


def test_selection_never_sees_test_dates() -> None:
    ev = FakeEvaluator({1: D("1"), 2: D("2")})
    result = walk_forward(ev, DAYS, {"x": 1}, {"x": [1, 2]}, train=4, test=2, step=2, holdout=2)
    train_dates = {d for dates, _ in ev.calls if len(dates) == 4 for d in dates}
    assert DAYS[8] not in train_dates
    assert DAYS[9] not in train_dates
    assert all(
        d not in (DAYS[8], DAYS[9]) for dates, _ in ev.calls for d in dates
    )  # the holdout is untouched
    assert len(result.folds) == 2


def test_ties_go_to_the_first_candidate_in_grid_order() -> None:
    ev = FakeEvaluator({1: D("2"), 2: D("2")})
    result = walk_forward(ev, DAYS, {"x": 1}, {"x": [1, 2]}, train=4, test=2, step=2)
    assert {f.chosen["x"] for f in result.folds} == {1}


def test_degradation_flags_an_overfit_selection() -> None:
    class Overfit(FakeEvaluator):
        def __call__(self, dates, params):  # type: ignore[no-untyped-def]
            ev = super().__call__(dates, params)
            sign = D(1) if len(dates) == 4 else D(-1)  # looks great in-sample, loses out-of-sample
            return Evaluation(
                **{
                    **ev.__dict__,
                    "net_pnl": ev.net_pnl * sign,
                    "daily_returns": tuple(r * sign for r in ev.daily_returns),
                }
            )

    result = walk_forward(Overfit({1: D("2")}), DAYS, {"x": 1}, {"x": [1]}, train=4, test=2, step=2)
    assert result.in_sample_net_pnl > 0 > result.out_of_sample_net_pnl
    assert result.degradation is not None
    assert result.degradation < 0
    assert result.verdict == "REJECTED"


def test_a_consistently_profitable_selection_is_not_rejected_and_is_corrected_for_multiple_tests() -> (
    None
):
    ev = FakeEvaluator({1: D("1"), 2: D("3")}, noise=D("0.0004"))
    result = walk_forward(ev, DAYS, {"x": 1}, {"x": [1, 2]}, train=4, test=2, step=2)
    assert result.out_of_sample_p_value is not None
    assert result.out_of_sample_p_value < D("0.05")
    assert result.verdict == "SUPPORTED"
    assert len(result.hypothesis_rejections) == result.hypotheses_tested


def test_too_little_data_is_inconclusive() -> None:
    result = walk_forward(FakeEvaluator({1: D(1)}), DAYS[:3], {"x": 1}, {"x": [1]}, train=4, test=2)
    assert result.folds == []
    assert result.verdict == "INCONCLUSIVE"


# ---- sensitivity ---------------------------------------------------------------------------------


def test_robust_parameters_survive_perturbation() -> None:
    ev = FakeEvaluator({9: D("1"), 10: D("1.1"), 11: D("0.9"), 12: D("1.0"), 8: D("0.95")})
    result = sensitivity(ev, DAYS, {"x": 10}, {"x": [8, 9, 11, 12]})
    assert result.base.params == {"x": 10}
    assert len(result.neighbors) == 4
    assert result.fraction_positive == D(1)
    assert result.robust is True


def test_a_knife_edge_optimum_is_flagged_fragile() -> None:
    ev = FakeEvaluator({10: D("2"), 9: D("-1"), 11: D("-1"), 8: D("-2"), 12: D("-1")})
    result = sensitivity(ev, DAYS, {"x": 10}, {"x": [8, 9, 11, 12]})
    assert result.fraction_positive == D(0)
    assert result.robust is False


def test_a_losing_base_is_never_robust() -> None:
    ev = FakeEvaluator({10: D("-1"), 9: D("1"), 11: D("1")})
    assert sensitivity(ev, DAYS, {"x": 10}, {"x": [9, 11]}).robust is False
