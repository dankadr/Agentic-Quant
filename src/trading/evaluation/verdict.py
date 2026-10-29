"""What a strategy comparison may claim. Strong words are reserved for evidence that earns them.

Verdicts: INCONCLUSIVE (not enough usable history or trades, so no conclusion is drawn),
NO_IMPROVEMENT (the candidate did not beat the baseline out of sample, net of costs), and
IMPROVEMENT_NOT_PROVEN (it did, on a small sample that cannot establish an edge). "Proven" is never
returned by this module.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

MIN_USABLE_SESSIONS = 60
MIN_TRADES = 30
COMPLETE_COVERAGE = Decimal("0.99")


class Verdict(StrEnum):
    INCONCLUSIVE = "INCONCLUSIVE"
    NO_IMPROVEMENT = "NO_IMPROVEMENT"
    IMPROVEMENT_NOT_PROVEN = "IMPROVEMENT_NOT_PROVEN"


@dataclass(frozen=True)
class Coverage:
    expected: tuple[date, ...]  # regular sessions, from the exchange calendar
    observed_fraction: dict[date, Decimal]  # complete bars per session, 0..1

    def usable(self) -> tuple[date, ...]:
        """A session counts only if its data is essentially complete.

        A missing session is not a zero return: it is simply absent from the sample."""
        return tuple(
            day
            for day in self.expected
            if self.observed_fraction.get(day, Decimal(0)) >= COMPLETE_COVERAGE
        )


@dataclass(frozen=True)
class Comparison:
    usable_sessions: int
    trades: int
    candidate_net: Decimal  # after estimated costs
    baseline_net: Decimal


def verdict(c: Comparison) -> Verdict:
    if c.usable_sessions < MIN_USABLE_SESSIONS or c.trades < MIN_TRADES:
        return Verdict.INCONCLUSIVE
    if c.candidate_net <= c.baseline_net:
        return Verdict.NO_IMPROVEMENT
    return Verdict.IMPROVEMENT_NOT_PROVEN
