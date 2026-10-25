import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.agents.llm import LlmError, LlmInvalidOutput, LlmResult
from trading.agents.technical import TechnicalOutput, run_technical_agent
from trading.core.config import load_config
from trading.quant.registry import seed_strategies
from trading.quant.research import record_experiment

CFG = load_config(Path("configs"))
D = Decimal
NOW = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
SPACE = {"target_r": ["1.0", "1.5", "2.0"]}


@dataclass
class FakeLlm:
    reply: object
    last_user: str = ""

    def complete_json(self, system: str, user: str, schema):  # type: ignore[no-untyped-def]
        self.last_user = user
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmResult(value=self.reply, model="fake", prompt_tokens=1, completion_tokens=1)


def experiments(db: Session, *, verdict: str = "SUPPORTED", robust: bool = True) -> None:
    record_experiment(db, kind="WALK_FORWARD", strategy_id="orb_v1", hypothesis="h", now=T0,
                      params={"base": {"target_r": "1.5"}, "space": SPACE, "train": 8, "test": 4},
                      data_range={"first": "2026-09-14", "last": "2026-10-23", "days": 30, "holdout": []},
                      metrics={"verdict": verdict, "chosen": [{"target_r": "2.0"}], "out_of_sample_net_pnl": D("12.5"),
                               "in_sample_net_pnl": D("20"), "degradation": D("0.6"), "p_value": D("0.01"),
                               "hypotheses_tested": 9, "folds": 3},
                      status="COMPLETED" if verdict == "SUPPORTED" else "REJECTED")  # fmt: skip
    record_experiment(db, kind="SENSITIVITY", strategy_id="orb_v1", hypothesis="h", now=T0,
                      params={"base": {"target_r": "1.5"}, "neighbors": {"target_r": ["1.0", "2.0"]}},
                      data_range={"first": "2026-09-14", "last": "2026-10-23", "days": 30, "holdout": []},
                      metrics={"robust": robust, "fraction_positive": D("1"), "base_net_pnl": D("5"), "neighbor_net_pnl": [D("4"), D("9")]},
                      status="COMPLETED" if robust else "REJECTED")  # fmt: skip
    db.commit()


def output(params: dict[str, object] | None = None) -> TechnicalOutput:
    proposal = (
        None if params is None else {"parameters": params, "rationale": "walk-forward supports 2.0"}
    )
    return TechnicalOutput.model_validate(
        {"assessment": "target_r 2.0 held out of sample", "proposal": proposal, "notes": []}
    )


@pytest.fixture(autouse=True)
def _seed(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    db.commit()


def run(sf: sessionmaker[Session], llm: FakeLlm) -> str:
    return run_technical_agent(sf, llm, config=CFG, strategy_id="orb_v1", now=NOW)  # type: ignore[arg-type]


def result(db: Session) -> dict[str, object]:
    return db.execute(
        text("select result_json from agent_runs where agent_name = 'technical'")
    ).scalar_one()


def proposals(db: Session) -> list[tuple[str, str, dict[str, object]]]:
    return [
        (r.status, r.proposed_by, r.parameters_json)
        for r in db.execute(
            text("select status, proposed_by, parameters_json from strategy_config_proposals")
        )
    ]


def test_a_supported_robust_in_grid_proposal_is_filed_for_human_review(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    experiments(db)
    llm = FakeLlm(output({"target_r": "2.0"}))
    assert run(session_factory, llm) == "COMPLETED"
    assert proposals(db) == [("PENDING_REVIEW", "agent:technical", {"target_r": "2.0"})]
    assert result(db)["proposal_filed"] is True
    payload = json.loads(llm.last_user)
    assert payload["walk_forward"]["verdict"] == "SUPPORTED"
    assert payload["current"]["params"]["target_r"] == "1.5"
    # live parameters are untouched: only a human approval + a new session can change them
    assert (
        db.execute(
            text("select params_json->>'target_r' from strategies where id = 'orb_v1'")
        ).scalar_one()
        == "1.5"
    )


@pytest.mark.parametrize(
    ("verdict", "robust", "params", "reason"),
    [
        ("REJECTED", True, {"target_r": "2.0"}, "walk_forward_not_supported"),
        ("INCONCLUSIVE", True, {"target_r": "2.0"}, "walk_forward_not_supported"),
        ("SUPPORTED", False, {"target_r": "2.0"}, "not_robust"),
        ("SUPPORTED", True, {"target_r": "3.7"}, "value_outside_tested_grid"),
        ("SUPPORTED", True, {"stop_loss": "1"}, "parameter_not_researched"),
        ("SUPPORTED", True, {"target_r": "1.5"}, "no_change"),
    ],
)
def test_proposals_that_the_evidence_does_not_support_are_never_filed(
    db: Session,
    session_factory: sessionmaker[Session],
    verdict: str,
    robust: bool,
    params: dict[str, object],
    reason: str,
) -> None:
    experiments(db, verdict=verdict, robust=robust)
    assert run(session_factory, FakeLlm(output(params))) == "COMPLETED"
    assert proposals(db) == []
    r = result(db)
    assert (r["proposal_filed"], r["reason"]) == (False, reason)


def test_no_proposal_from_the_model_means_notes_only(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    experiments(db)
    assert run(session_factory, FakeLlm(output(None))) == "COMPLETED"
    assert proposals(db) == []
    assert result(db)["reason"] == "no_proposal"


def test_float_parameters_are_invalid_model_output() -> None:
    with pytest.raises(ValueError):
        output({"target_r": 2.0})


def test_without_experiments_the_model_is_not_called(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    llm = FakeLlm(output({"target_r": "2.0"}))
    assert run(session_factory, llm) == "FAILED"
    assert llm.last_user == ""
    assert "no_experiments" in json.dumps(result(db))


def test_model_failures_are_recorded_and_file_nothing(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    experiments(db)
    assert run(session_factory, FakeLlm(LlmError("down"))) == "FAILED"
    assert run(session_factory, FakeLlm(LlmInvalidOutput("junk"))) == "REJECTED_INVALID"
    assert proposals(db) == []
