from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from trading.agents.evaluator import EvaluatorOutput, render_review
from trading.monitoring.report import percentage_display


def test_fraction_display_multiplies_by_one_hundred_once() -> None:
    assert percentage_display(D("-0.007526")) == "-0.7526%"
    assert percentage_display(None) is None


def test_model_explanations_reference_deterministic_metrics() -> None:
    report = {
        "date": "2026-11-13",
        "ticks": 390,
        "equity": {
            "pnl": "-54.1438",
            "return_pct_display": "-0.0541%",
            "max_drawdown_pct_display": "0.1074%",
        },
        "benchmark": {"symbol": "SPY", "return_pct_display": "-0.7526%"},
        "costs": {"fees": "1.5518", "slippage_estimate": "44.6467"},
        "data_coverage": {},
        "strategies": {},
    }
    out = EvaluatorOutput(
        findings=[
            {
                "metric_id": "benchmark.return",
                "explanation_code": "SMALL_SAMPLE",
            }
        ],
        suggestions=["REVIEW_EXECUTION"],
    )
    review = render_review(report, out)
    assert "-0.7526%" in review["findings"][0]
    assert "-54.1438" in review["summary"]
    assert "44.6467" in review["facts"]["costs.slippage"]
    assert "already reflected" in review["facts"]["costs.slippage"]


def test_numeric_model_claims_and_unknown_metric_references_are_rejected() -> None:
    with pytest.raises(ValidationError):
        EvaluatorOutput(
            findings=[{"metric_id": "benchmark.return", "explanation": "SPY fell 0.008%."}]
        )
    with pytest.raises(ValueError, match="unknown metric"):
        render_review(
            {"date": "2026-11-13", "ticks": 0},
            EvaluatorOutput(
                findings=[{"metric_id": "invented", "explanation_code": "OBSERVATION"}]
            ),
        )


def test_spelled_out_fabricated_amounts_cannot_enter_the_review() -> None:
    with pytest.raises(ValidationError):
        EvaluatorOutput(
            findings=[{"metric_id": "equity.pnl", "explanation": "lost one million dollars"}]
        )
    with pytest.raises(ValidationError):
        EvaluatorOutput(suggestions=["Loss is one million dollars; double the size."])
