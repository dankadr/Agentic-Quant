"""Property tests for the invariants most worth never breaking."""

import json
from datetime import date, timedelta
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from trading.agents.llm import _extract_json
from trading.quant.research import make_folds
from trading.quant.stats import holm_bonferroni

D = Decimal


@given(
    n=st.integers(1, 80),
    train=st.integers(1, 20),
    test=st.integers(1, 10),
    step=st.integers(1, 10),
    holdout=st.integers(0, 15),
)
def test_folds_never_leak_the_future_or_touch_the_holdout(
    n: int, train: int, test: int, step: int, holdout: int
) -> None:
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(n)]
    folds, held = make_folds(days, train=train, test=test, step=step, holdout=holdout)
    assert len(held) == min(holdout, n)
    for f in folds:
        assert len(f.train) == train and len(f.test) == test
        assert max(f.train) < min(f.test)  # training strictly precedes testing
        assert not set(f.train) & set(f.test)
        assert not (set(f.train) | set(f.test)) & set(held)  # the holdout is never searched
    assert [f.test[0] for f in folds] == sorted({f.test[0] for f in folds})  # windows roll forward


@settings(deadline=None)
@given(
    ps=st.lists(st.decimals(min_value=D("0"), max_value=D("1"), places=4), min_size=1, max_size=30),
    alpha=st.sampled_from([D("0.01"), D("0.05"), D("0.1")]),
)
def test_holm_rejects_a_prefix_of_the_sorted_p_values_and_never_more_than_raw_alpha(
    ps: list[Decimal], alpha: Decimal
) -> None:
    rejected = holm_bonferroni(ps, alpha=alpha)
    assert len(rejected) == len(ps)
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    flags = [rejected[i] for i in order]
    assert flags == sorted(flags, reverse=True)  # once one is kept, every larger p is kept too
    assert all(
        ps[i] <= alpha for i in range(len(ps)) if rejected[i]
    )  # never rejects beyond the uncorrected level
    if rejected.count(True) and len(ps) > 1:
        assert min(ps) <= alpha / len(ps)  # the first rejection needs p <= alpha/m


@given(
    payload=st.dictionaries(
        st.text(st.characters(whitelist_categories=("L", "N")), min_size=1, max_size=8),
        st.one_of(st.integers(), st.booleans(), st.text(max_size=12)),
        min_size=1,
        max_size=5,
    ),
    prefix=st.text(alphabet="abc [],.:\n", max_size=30),
    suffix=st.text(alphabet="abc [],.:\n", max_size=30),
)
def test_a_json_object_is_recovered_from_surrounding_prose(
    payload: dict[str, object], prefix: str, suffix: str
) -> None:
    assert _extract_json(prefix + json.dumps(payload) + suffix) == payload
