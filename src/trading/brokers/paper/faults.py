"""Deterministic fault injection for the paper broker (tests and chaos drills)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultPlan:
    timeout_after_accept: frozenset[str] = frozenset()  # order exists, caller sees a timeout
    timeout_before_accept: frozenset[str] = frozenset()  # order never created, caller sees timeout
    unavailable: bool = False
