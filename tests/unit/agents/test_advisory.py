from datetime import UTC, datetime
from decimal import Decimal as D

from trading.agents.advisory import ActivityCandidate, quality_findings, rank_activity

NOW = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)


def test_quality_detects_coverage_and_stale_quotes_separately():
    findings = quality_findings(
        {
            "SPY": {"bars": 29, "expected": 30, "flagged": 0, "quote_age_seconds": 5},
            "DIA": {"bars": 10, "expected": 30, "flagged": 2, "quote_age_seconds": 91},
            "QQQ": {"bars": 0, "expected": 0, "flagged": 0, "quote_age_seconds": None},
        },
        quote_max_age_seconds=90,
    )
    assert findings == [
        "DIA: coverage below 95%",
        "DIA: flagged bars present",
        "DIA: missing or stale quote",
        "QQQ: missing or stale quote",
    ]


def test_activity_ranking_uses_both_factors_and_ties_by_symbol():
    candidates = [
        ActivityCandidate("NVDA", D(".1"), D("1"), D("2"), NOW),
        ActivityCandidate("SPY", D("-.01"), D("5"), D("1"), NOW),
        ActivityCandidate("AAPL", D(".05"), D("3"), D("1"), NOW),
        ActivityCandidate("MSFT", D(".05"), D("3"), D("1"), NOW),
    ]
    result = rank_activity(candidates)
    assert [item["symbol"] for item in result] == ["AAPL", "MSFT", "NVDA", "SPY"]
    assert result[0]["direction"] == "UP"
    assert result[-1]["direction"] == "DOWN"
    assert result[0]["score"] == "0.6666666666666666666666666665"


def test_activity_returns_top_five_only():
    rows = [ActivityCandidate(f"A{i}", D(i), D(i), D("1"), NOW) for i in range(7)]
    assert [r["symbol"] for r in rank_activity(rows)] == ["A6", "A5", "A4", "A3", "A2"]
