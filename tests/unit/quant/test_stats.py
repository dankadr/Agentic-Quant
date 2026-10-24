from decimal import Decimal

import pytest

from trading.quant.stats import holm_bonferroni, mean_return_p_value, student_t_sf

D = Decimal


@pytest.mark.parametrize(
    ("t", "df", "expected"),
    [
        (0.0, 10, 0.5),
        (2.0, 10, 0.03669),
        (1.0, 5, 0.18161),
        (-1.5, 20, 0.92554),
        (3.0, 3, 0.02884),
        (2.0, 1000, 0.02300),
    ],
)
def test_student_t_survival_matches_reference_values(t: float, df: int, expected: float) -> None:
    assert student_t_sf(t, df) == pytest.approx(expected, abs=2e-4)


def test_student_t_sf_is_monotone_and_bounded() -> None:
    values = [student_t_sf(x / 2, 8) for x in range(-10, 11)]
    assert values == sorted(values, reverse=True)
    assert all(0 < v < 1 for v in values)


def test_mean_return_p_value() -> None:
    steady = [D("0.01"), D("0.012"), D("0.009"), D("0.011"), D("0.01")]
    noise = [D("0.01"), D("-0.01"), D("0.012"), D("-0.011"), D("0.001")]
    p_steady = mean_return_p_value(steady)
    p_noise = mean_return_p_value(noise)
    assert p_steady is not None
    assert p_noise is not None
    assert p_steady < D("0.001")  # consistently positive: strong evidence
    assert p_noise > D("0.3")  # indistinguishable from zero
    assert isinstance(p_steady, Decimal)


def test_p_value_undefined_without_enough_data_or_dispersion() -> None:
    assert mean_return_p_value([D("0.01")]) is None
    assert mean_return_p_value([D("0.01"), D("0.01"), D("0.01")]) is None  # zero variance


def test_negative_edge_gets_a_large_p_value() -> None:
    p = mean_return_p_value([D("-0.01"), D("-0.02"), D("-0.015"), D("-0.012")])
    assert p is not None
    assert p > D("0.95")


def test_holm_bonferroni_controls_family_wise_error() -> None:
    p = [D("0.001"), D("0.04"), D("0.03"), D("0.20")]
    # sorted: 0.001 <= 0.05/4, 0.03 > 0.05/3 -> stop. Only the first survives.
    assert holm_bonferroni(p, alpha=D("0.05")) == [True, False, False, False]
    assert holm_bonferroni([D("0.01"), D("0.02")], alpha=D("0.05")) == [True, True]
    assert holm_bonferroni([D("0.03"), D("0.04")], alpha=D("0.05")) == [
        False,
        False,
    ]  # 0.03 > 0.05 / 2
    assert holm_bonferroni([], alpha=D("0.05")) == []


def test_holm_preserves_input_order_and_handles_ties() -> None:
    assert holm_bonferroni([D("0.2"), D("0.001"), D("0.001")], alpha=D("0.05")) == [
        False,
        True,
        True,
    ]
