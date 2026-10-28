from decimal import Decimal

from trading.reports.compare import FactPoint, compare
from trading.reports.contracts import ReportOutput
from trading.reports.verify import verify

PAGE = "Revenue rose 12 percent to 4.2 billion dollars. Guidance was raised."
PAGES = [PAGE]


def _quote(start_text: str, length: int) -> tuple[int, int, str]:
    start = PAGE.index(start_text)
    return start, start + length, PAGE[start : start + length]


def _valid() -> dict[str, object]:
    start, end, quote = _quote("4.2 billion", len("4.2 billion"))
    return {
        "facts": [
            {
                "key": "revenue",
                "value": "4.2",
                "unit": "USD_MILLIONS",
                "period": "2026Q3",
                "basis": "UNSTATED",
                "citation": {"page": 1, "start": start, "end": end, "quote": quote},
            }
        ],
        "claims": [
            {
                "kind": "SOURCE_FACT",
                "text": "Revenue rose to 4.2 billion dollars.",
                "citations": [{"page": 1, "start": start, "end": end, "quote": quote}],
            }
        ],
        "limitations": ["no segment detail"],
        "missing": [],
    }


def test_a_faithful_report_has_no_violations() -> None:
    assert verify(ReportOutput.model_validate(_valid()), PAGES) == []


def test_a_quote_that_does_not_match_the_source_is_caught() -> None:
    data = _valid()
    fact = data["facts"][0]  # type: ignore[index]
    fact["citation"]["quote"] = "a sentence the document never says"  # type: ignore[index]
    problems = verify(ReportOutput.model_validate(data), PAGES)
    assert any("quote does not match" in p for p in problems)


def test_a_citation_to_a_page_that_does_not_exist_is_caught() -> None:
    data = _valid()
    data["facts"][0]["citation"]["page"] = 7  # type: ignore[index]
    assert any("does not exist" in p for p in verify(ReportOutput.model_validate(data), PAGES))


def test_a_number_not_present_in_its_own_quote_is_caught() -> None:
    data = _valid()
    data["facts"][0]["value"] = "9.9"  # type: ignore[index]
    problems = verify(ReportOutput.model_validate(data), PAGES)
    assert any("does not appear in the cited quote" in p for p in problems)


def test_a_source_fact_without_a_citation_is_caught() -> None:
    data = _valid()
    data["claims"][0]["citations"] = []  # type: ignore[index]
    assert any("must cite" in p for p in verify(ReportOutput.model_validate(data), PAGES))


def test_an_interpretation_may_not_introduce_a_new_number() -> None:
    data = _valid()
    data["claims"].append(  # type: ignore[union-attr]
        {"kind": "MODEL_INTERPRETATION", "text": "Margins may reach 31 percent.", "citations": []}
    )
    problems = verify(ReportOutput.model_validate(data), PAGES)
    assert any("introduces a number" in p for p in problems)


def _point(**overrides: object) -> FactPoint:
    base: dict[str, object] = dict(
        company="AAPL", key="revenue", value=Decimal("100"), unit="USD_MILLIONS",
        period="2026Q2", basis="GAAP",
    )  # fmt: skip
    base.update(overrides)
    return FactPoint(**base)  # type: ignore[arg-type]


def test_a_comparable_pair_gives_an_exact_change() -> None:
    result = compare(_point(value=Decimal("112")), _point(value=Decimal("100"), period="2025Q3"))
    assert result is not None
    assert (result.change, result.change_pct) == (Decimal("12"), Decimal("12"))


def test_different_basis_or_unit_is_never_compared() -> None:
    assert compare(_point(basis="NON_GAAP"), _point(basis="GAAP", period="2025Q3")) is None
    assert compare(_point(unit="PERCENT"), _point(unit="USD_MILLIONS", period="2025Q3")) is None


def test_a_zero_prior_has_a_change_but_no_meaningless_percentage() -> None:
    result = compare(_point(value=Decimal("5")), _point(value=Decimal("0"), period="2025Q3"))
    assert result is not None
    assert (result.change, result.change_pct) == (Decimal("5"), None)
