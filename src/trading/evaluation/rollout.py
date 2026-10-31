"""The operational gate for controlled paper rollout.

It passes only after N consecutive sessions in which every criterion held. A zero-trade day counts
as a clean day for uptime, but it cannot prove order handling, so the gate also requires that the
deliberate acceptance scenarios passed during the window.
"""

from dataclasses import dataclass
from datetime import date

REQUIRED_SESSIONS = 20


@dataclass(frozen=True)
class SessionRecord:
    day: date
    open_positions_at_close: int
    unexplained_discrepancies: int
    duplicate_executions: int
    uncontrolled_orders: int
    trades: int


@dataclass(frozen=True)
class Gate:
    verdict: str  # PASS | FAIL | INCONCLUSIVE
    consecutive_clean: int
    first_failure: date | None
    acceptance_scenarios_passed: bool
    reasons: tuple[str, ...]


def _clean(r: SessionRecord) -> bool:
    return (
        r.open_positions_at_close == 0
        and r.unexplained_discrepancies == 0
        and r.duplicate_executions == 0
        and r.uncontrolled_orders == 0
    )


def gate(records: list[SessionRecord], *, acceptance_scenarios_passed: bool) -> Gate:
    ordered = sorted(records, key=lambda r: r.day)
    failure = next((r for r in reversed(ordered) if not _clean(r)), None)
    run = 0
    for record in reversed(ordered):
        if not _clean(record):
            break
        run += 1
    reasons: list[str] = []
    if failure is not None:
        reasons.append(f"session {failure.day.isoformat()} failed a criterion")
    if run < REQUIRED_SESSIONS:
        reasons.append(f"{run} of {REQUIRED_SESSIONS} consecutive clean sessions")
    if not acceptance_scenarios_passed:
        reasons.append("deliberate paper acceptance scenarios have not all passed")
    if failure is not None and run == 0:
        verdict = "FAIL"
    elif reasons:
        verdict = "INCONCLUSIVE"
    else:
        verdict = "PASS"
    return Gate(
        verdict=verdict,
        consecutive_clean=run,
        first_failure=failure.day if failure else None,
        acceptance_scenarios_passed=acceptance_scenarios_passed,
        reasons=tuple(reasons),
    )
