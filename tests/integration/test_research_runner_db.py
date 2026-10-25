from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from trading.core.config import load_config
from trading.quant.registry import seed_strategies
from trading.quant.research_runner import HoldoutAlreadyUsed, run_research

CFG = load_config(Path("configs"))
D = Decimal
DAYS = [date(2026, 9, 1) + timedelta(days=i) for i in range(20)]
NOW = datetime(2026, 10, 25, tzinfo=UTC)


def good_full_run(params):  # type: ignore[no-untyped-def]
    # target_r 2.0 is consistently good, the others mildly negative
    per_day = D("0.002") if params["target_r"] == "2.0" else D("-0.0005")
    return {d: per_day for d in DAYS}


def go(sf: sessionmaker[Session], **kw):  # type: ignore[no-untyped-def]
    return run_research(
        sf,
        good_full_run,
        strategy_id="orb_v1",
        dates=DAYS,
        base_params={"target_r": "1.5"},
        space={"target_r": ["1.0", "1.5", "2.0"]},
        neighbors={"target_r": ["1.0", "2.0"]},
        train=8,
        test=4,
        holdout=4,
        base_equity=D(1000),
        now=NOW,
        **kw,
    )


def test_research_records_walk_forward_and_sensitivity_experiments(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seed_strategies(db, CFG, now=T0)
    db.commit()
    outcome = go(session_factory)
    assert outcome.walk_forward.folds
    rows = db.execute(
        text(
            "select kind, status, params_json, data_range_json, metrics_json from experiments order by kind"
        )
    ).all()
    assert [r.kind for r in rows] == ["SENSITIVITY", "WALK_FORWARD"]
    wf = next(r for r in rows if r.kind == "WALK_FORWARD")
    assert wf.metrics_json["chosen"] == [{"target_r": "2.0"}] * len(outcome.walk_forward.folds)
    assert wf.data_range_json["holdout"] == [d.isoformat() for d in DAYS[-4:]]
    assert wf.params_json["space"] == {"target_r": ["1.0", "1.5", "2.0"]}
    assert wf.metrics_json["verdict"] in ("SUPPORTED", "INCONCLUSIVE")
    assert wf.status == (
        "COMPLETED" if wf.metrics_json["verdict"] == "SUPPORTED" else "INCONCLUSIVE"
    )


def test_the_final_holdout_is_evaluated_once_and_never_again(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seed_strategies(db, CFG, now=T0)
    db.commit()
    first = go(session_factory, evaluate_holdout=True)
    assert first.holdout is not None
    assert first.holdout.params == {"target_r": "2.0"}
    assert len(first.holdout.dates) == 4
    with pytest.raises(HoldoutAlreadyUsed):
        go(session_factory, evaluate_holdout=True)
    assert go(session_factory).holdout is None  # without the flag the holdout stays untouched
    kinds = db.execute(
        text("select count(*) from experiments where notes like '%holdout%'")
    ).scalar_one()
    assert kinds == 1
