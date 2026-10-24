"""Statistical helpers for research. These are analysis statistics, not money, so they use
floats internally (math module) and return Decimal at the boundary."""

import math
from collections.abc import Sequence
from decimal import Decimal


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def student_t_sf(t: float, df: int) -> float:
    """P(T > t) for Student's t with ``df`` degrees of freedom."""
    x = df / (df + t * t)
    tail = 0.5 * _betainc(df / 2.0, 0.5, x)
    return tail if t > 0 else 1.0 - tail


def mean_return_p_value(returns: Sequence[Decimal]) -> Decimal | None:
    """One-sided p-value for H0: mean return <= 0 (one-sample t-test). None if undefined."""
    n = len(returns)
    if n < 2:
        return None
    values = [float(r) for r in returns]
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    if variance <= 0:
        return None
    t = mean / math.sqrt(variance / n)
    return Decimal(str(round(student_t_sf(t, n - 1), 10)))


def holm_bonferroni(p_values: Sequence[Decimal], *, alpha: Decimal) -> list[bool]:
    """Holm step-down: which hypotheses are rejected while controlling family-wise error.

    Testing many parameter combinations inflates false positives; this is the correction the
    spec requires. Results are returned in the input order.
    """
    m = len(p_values)
    order = sorted(range(m), key=lambda i: (p_values[i], i))
    rejected = [False] * m
    for rank, index in enumerate(order):
        if p_values[index] <= alpha / (m - rank):
            rejected[index] = True
        else:
            break
    return rejected
