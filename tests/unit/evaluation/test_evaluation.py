from datetime import date
from decimal import Decimal
from pathlib import Path

from trading.evaluation.research_eval import (
    CATEGORIES,
    MIN_DOCUMENTS,
    MIN_PER_CATEGORY,
    Case,
    evaluate,
    load,
    release_criteria,
)
from trading.evaluation.rollout import SessionRecord, gate
from trading.evaluation.verdict import (
    MIN_USABLE_SESSIONS,
    Comparison,
    Coverage,
    Verdict,
    verdict,
)

SEED = Path("evals/research/v1.json")


def test_the_seed_set_detects_fabrication_and_refuses_the_injection() -> None:
    result = evaluate(load(SEED))
    # The invented quote is a fabricated citation; the wrong number is caught as a mismatch.
    assert result.fabricated_citations == 1
    assert any("value 9.9" in p for p in result.problems)
    assert result.malicious_accepted == 0
    assert result.accepted == 1  # only the faithful report passes verification


def test_the_seed_set_can_never_release_and_says_why() -> None:
    result = evaluate(load(SEED))
    release = release_criteria(result, supported_claim_rate=None)
    # A fabricated citation is a failure outright; too few real cases is a shortfall on top.
    assert release.verdict == "FAIL"
    assert any("do not resolve" in r for r in release.reasons)
    assert any("real documents" in r for r in release.reasons)


def _cases(
    n_per_category: int, *, fabricated: bool = False, malicious_accepted: bool = False
) -> list[Case]:
    page = "Revenue rose 12 percent to 4.2 billion dollars."
    quote = "4.2 billion"
    start = page.index(quote)
    cite = {"page": 1, "start": start, "end": start + len(quote), "quote": quote}
    if fabricated:
        cite = {**cite, "quote": "something else"}
    candidate = {"facts": [{"key": "revenue", "value": "4.2", "unit": "USD_MILLIONS", "period": "2026Q3",
                            "basis": "UNSTATED", "citation": cite}], "claims": [], "limitations": [],
                 "missing": []}  # fmt: skip
    cases = []
    for category in CATEGORIES:
        for i in range(n_per_category):
            cases.append(
                Case(f"{category}-{i}", category, (page,), candidate, False, synthetic=False)
            )
    if malicious_accepted:
        cases.append(Case("inj", "MANAGEMENT_CALL", (page,), candidate, True, synthetic=False))
    return cases


def test_real_cases_that_meet_every_criterion_release_with_human_review() -> None:
    result = evaluate(_cases(MIN_PER_CATEGORY))
    assert result.real_cases >= MIN_DOCUMENTS
    assert release_criteria(result, supported_claim_rate=0.97).verdict == "PASS"


def test_a_low_supported_rate_or_any_fabrication_fails_outright() -> None:
    assert (
        release_criteria(evaluate(_cases(MIN_PER_CATEGORY)), supported_claim_rate=0.90).verdict
        == "FAIL"
    )
    assert (
        release_criteria(
            evaluate(_cases(MIN_PER_CATEGORY, fabricated=True)), supported_claim_rate=1.0
        ).verdict
        == "FAIL"
    )
    assert release_criteria(evaluate(_cases(MIN_PER_CATEGORY, malicious_accepted=True)), supported_claim_rate=1.0).verdict == "FAIL"  # fmt: skip


def test_verdicts_never_claim_proof_and_need_enough_history() -> None:
    small = Comparison(usable_sessions=MIN_USABLE_SESSIONS - 1, trades=100,
                       candidate_net=Decimal("50"), baseline_net=Decimal("0"))  # fmt: skip
    assert verdict(small) is Verdict.INCONCLUSIVE
    better = Comparison(usable_sessions=MIN_USABLE_SESSIONS, trades=100,
                        candidate_net=Decimal("50"), baseline_net=Decimal("0"))  # fmt: skip
    assert verdict(better) is Verdict.IMPROVEMENT_NOT_PROVEN
    worse = Comparison(usable_sessions=MIN_USABLE_SESSIONS, trades=100,
                       candidate_net=Decimal("-5"), baseline_net=Decimal("0"))  # fmt: skip
    assert verdict(worse) is Verdict.NO_IMPROVEMENT


def test_a_missing_session_is_absent_not_a_zero_return() -> None:
    days = (date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21))
    coverage = Coverage(days, {days[0]: Decimal("1"), days[1]: Decimal("0.5")})
    assert coverage.usable() == (days[0],)


def _records(n_clean: int, *, failing_last: bool = False) -> list[SessionRecord]:
    out: list[SessionRecord] = []
    base = date(2026, 10, 1)
    for i in range(n_clean):
        out.append(SessionRecord(date.fromordinal(base.toordinal() + i), 0, 0, 0, 0, 1))
    if failing_last:
        out.append(SessionRecord(date.fromordinal(base.toordinal() + n_clean), 1, 0, 0, 0, 0))
    return out


def test_rollout_passes_only_after_twenty_clean_sessions_and_acceptance() -> None:
    assert gate(_records(20), acceptance_scenarios_passed=True).verdict == "PASS"
    assert gate(_records(19), acceptance_scenarios_passed=True).verdict == "INCONCLUSIVE"
    assert gate(_records(20), acceptance_scenarios_passed=False).verdict == "INCONCLUSIVE"


def test_an_unresolved_position_at_close_fails_the_gate_until_clean_again() -> None:
    failing = gate(_records(25, failing_last=True), acceptance_scenarios_passed=True)
    assert failing.verdict == "FAIL" and failing.consecutive_clean == 0
    recovered = gate(_records(25), acceptance_scenarios_passed=True)
    assert recovered.verdict == "PASS"
