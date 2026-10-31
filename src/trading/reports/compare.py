"""Deterministic comparison of two facts. Comparable only if everything but the period matches."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class FactPoint:
    company: str | None
    key: str
    value: Decimal
    unit: str
    period: str
    basis: str


@dataclass(frozen=True)
class Comparison:
    current: FactPoint
    prior: FactPoint
    change: Decimal
    change_pct: Decimal | None  # None when the prior value is zero: a ratio would be meaningless


def compare(current: FactPoint, prior: FactPoint) -> Comparison | None:
    """A change only when company, key, unit and accounting basis agree and the periods differ.

    Anything else is not comparable, and None is returned rather than a guessed change.
    """
    same_series = (
        current.company == prior.company
        and current.key == prior.key
        and current.unit == prior.unit
        and current.basis == prior.basis
    )
    if not same_series or current.period == prior.period:
        return None
    change = current.value - prior.value
    pct = None if prior.value == 0 else (change / abs(prior.value)) * Decimal(100)
    return Comparison(current=current, prior=prior, change=change, change_pct=pct)
