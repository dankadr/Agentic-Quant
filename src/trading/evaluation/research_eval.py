"""Research-quality evaluation: every candidate report is checked against the stored source text.

A case is a document (its pages) with a candidate answer, or an input that must be refused
(malicious or malformed). The same ``verify`` used in production scores each case, so the
evaluation measures exactly what the system enforces. Release criteria come from the plan and are
never relaxed to pass: too few cases means INCONCLUSIVE, not PASS.
"""

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trading.reports.contracts import ReportOutput
from trading.reports.verify import verify

MIN_DOCUMENTS = 40
MIN_PER_CATEGORY = 10
MIN_SUPPORTED_CLAIM_RATE = 0.95
CATEGORIES = ("FINANCIAL_STATEMENT", "ANALYST_REPORT", "MACRO_RELEASE", "MANAGEMENT_CALL")


@dataclass(frozen=True)
class Case:
    case_id: str
    category: str
    pages: tuple[str, ...]
    candidate: dict[str, Any] | None  # None: the input must be refused, not answered
    malicious: bool
    synthetic: bool


@dataclass
class Result:
    cases: int = 0
    accepted: int = 0
    refused: int = 0
    malicious_accepted: int = 0
    fabricated_citations: int = 0
    citations_checked: int = 0
    per_category: Counter[str] = field(default_factory=Counter)
    synthetic_cases: int = 0
    real_cases: int = 0
    problems: list[str] = field(default_factory=list)


# Problems that mean a citation points at text that is not in the document.
_FABRICATION = ("quote does not match", "outside", "does not exist")


def load(path: Path) -> list[Case]:
    raw = json.loads(path.read_text())
    cases: list[Case] = []
    for item in raw["cases"]:
        cases.append(
            Case(
                case_id=str(item["id"]),
                category=str(item["category"]),
                pages=tuple(str(p) for p in item["pages"]),
                candidate=item.get("candidate"),
                malicious=bool(item.get("malicious", False)),
                synthetic=bool(item.get("synthetic", True)),
            )
        )
    return cases


def evaluate(cases: list[Case]) -> Result:
    result = Result()
    for case in cases:
        result.cases += 1
        result.per_category[case.category] += 1
        result.synthetic_cases += case.synthetic
        result.real_cases += not case.synthetic
        if case.candidate is None:
            result.refused += 1  # an input that must not be answered, and was not
            continue
        output = ReportOutput.model_validate(case.candidate)
        result.citations_checked += len(output.facts) + sum(len(c.citations) for c in output.claims)
        problems = verify(output, list(case.pages))
        result.fabricated_citations += sum(1 for p in problems if any(f in p for f in _FABRICATION))
        if problems:
            result.refused += 1
            result.problems.extend(f"{case.case_id}: {p}" for p in problems)
        else:
            result.accepted += 1
            if case.malicious:
                result.malicious_accepted += 1
                result.problems.append(f"{case.case_id}: malicious input was accepted")
    return result


@dataclass(frozen=True)
class Release:
    verdict: str  # PASS | FAIL | INCONCLUSIVE
    reasons: tuple[str, ...]


def release_criteria(result: Result, *, supported_claim_rate: float | None) -> Release:
    """The plan's gate. A missing human-review rate is INCONCLUSIVE, never a pass."""
    failures: list[str] = []
    shortfalls: list[str] = []
    if result.real_cases < MIN_DOCUMENTS:
        shortfalls.append(f"needs {MIN_DOCUMENTS} real documents, has {result.real_cases}")
    for category in CATEGORIES:
        if result.per_category[category] < MIN_PER_CATEGORY:
            shortfalls.append(f"{category} needs {MIN_PER_CATEGORY} real cases")
    if result.fabricated_citations:
        failures.append(f"{result.fabricated_citations} citation(s) do not resolve")
    if result.malicious_accepted:
        failures.append(f"{result.malicious_accepted} malicious input(s) were accepted")
    if supported_claim_rate is None:
        shortfalls.append("no human review of sampled claims")
    elif supported_claim_rate < MIN_SUPPORTED_CLAIM_RATE:
        failures.append(f"supported claim rate {supported_claim_rate:.2f} below 0.95")
    if failures:
        return Release("FAIL", tuple(failures + shortfalls))
    if shortfalls:
        return Release("INCONCLUSIVE", tuple(shortfalls))
    return Release("PASS", ())
