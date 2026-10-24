from decimal import Decimal

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.factories import T0, full_chain, make_fill
from trading.db.models.portfolio import StrategyAttribution
from trading.db.models.research import Experiment, Strategy, StrategyConfigProposal


def _strategy(db: Session, sid: str = "orb_v1") -> None:
    db.add(
        Strategy(
            id=sid,
            name=sid,
            version="1.0.0",
            enabled=True,
            capital_budget=Decimal(200),
            params_json={},
            created_at=T0,
        )
    )
    db.flush()


def test_new_tables_exist(db: Session) -> None:
    names = set(inspect(db.get_bind()).get_table_names())
    assert {"strategy_config_proposals", "experiments"} <= names


def test_proposal_status_is_constrained(db: Session) -> None:
    _strategy(db)
    ok = StrategyConfigProposal(strategy_id="orb_v1", base_version="1.0.0", parameters_json={"target_r": "2"},
                                rationale="test", status="PENDING_REVIEW", proposed_by="op", created_at=T0)  # fmt: skip
    db.add(ok)
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(text("update strategy_config_proposals set status = 'LIVE'"))


def test_proposal_requires_known_strategy(db: Session) -> None:
    db.add(StrategyConfigProposal(strategy_id="ghost", base_version="1", parameters_json={}, rationale="x",
                                  status="PENDING_REVIEW", proposed_by="op", created_at=T0))  # fmt: skip
    with pytest.raises(IntegrityError):
        db.flush()


def test_experiment_kinds_and_status_are_constrained(db: Session) -> None:
    db.add(Experiment(kind="BACKTEST", strategy_id=None, hypothesis="h", params_json={}, data_range_json={},
                      metrics_json={}, status="COMPLETED", notes="", created_at=T0))  # fmt: skip
    db.flush()
    with pytest.raises(IntegrityError):
        db.execute(text("update experiments set kind = 'LIVE_TRADING'"))


def test_attribution_is_unique_per_strategy_and_fill(db: Session) -> None:
    _strategy(db)
    _, _, _, _, order = full_chain(db)
    fill = make_fill(db, order)
    row = dict(
        strategy_id="orb_v1",
        fill_id=fill.id,
        allocated_qty=Decimal("0.1"),
        allocated_fees=Decimal("0"),
        pnl=None,
    )
    db.add(StrategyAttribution(**row))
    db.flush()
    db.add(StrategyAttribution(**row))
    with pytest.raises(IntegrityError):
        db.flush()
