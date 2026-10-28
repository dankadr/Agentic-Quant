"""Deterministic checks on a model's report, against the stored source text.

A report that fails any check is rejected whole. Partial acceptance would let a single fabricated
citation hide among true ones.
"""

import re
from decimal import Decimal, InvalidOperation

from trading.reports.contracts import ReportOutput

_DIGITS = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[Decimal]:
    found: set[Decimal] = set()
    for token in _DIGITS.findall(text):
        try:
            found.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def verify(output: ReportOutput, pages: list[str]) -> list[str]:
    """Violations, one per problem. An empty list means every claim is supported by the source."""
    problems: list[str] = []

    def check_span(label: str, page: int, start: int, end: int, quote: str) -> bool:
        if not 1 <= page <= len(pages):
            problems.append(f"{label}: page {page} does not exist")
            return False
        text = pages[page - 1]
        if not 0 <= start < end <= len(text):
            problems.append(f"{label}: span {start}-{end} is outside page {page}")
            return False
        if text[start:end] != quote:
            problems.append(f"{label}: quote does not match the source at that span")
            return False
        return True

    for index, fact in enumerate(output.facts):
        label = f"fact {index} ({fact.key})"
        cite = fact.citation
        if not check_span(label, cite.page, cite.start, cite.end, cite.quote):
            continue
        try:
            value = Decimal(fact.value.replace(",", ""))
        except InvalidOperation:
            problems.append(f"{label}: value {fact.value!r} is not a number")
            continue
        if value not in _numbers(cite.quote):
            problems.append(f"{label}: value {fact.value} does not appear in the cited quote")

    for index, claim in enumerate(output.claims):
        label = f"claim {index} ({claim.kind})"
        if claim.kind == "SOURCE_FACT" and not claim.citations:
            problems.append(f"{label}: a source fact must cite its source")
        for cite in claim.citations:
            check_span(label, cite.page, cite.start, cite.end, cite.quote)
        if claim.kind == "MODEL_INTERPRETATION":
            stated = _numbers(claim.text)
            allowed = {value for fact in output.facts for value in _numbers(fact.value)}
            if stated - allowed:
                problems.append(f"{label}: introduces a number that no fact supports")
    return problems
