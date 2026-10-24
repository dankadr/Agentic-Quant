from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.core.config import load_config
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))


def test_seed_strategies_is_idempotent_and_budgeted(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    seed_strategies(db, CFG, now=T0)
    rows = {
        r[0]: r
        for r in db.execute(
            text("select id, version, enabled, capital_budget, params_json from strategies")
        ).all()
    }
    assert "cash_reserve" not in rows
    assert {
        "orb_v1",
        "vwap_v1",
        "eod_flatten",
        "macro_research",
        "fundamental_watchlist",
        "consensus_shadow",
    } == set(rows)
    assert rows["orb_v1"].capital_budget == 200  # 20% of the $1,000 starting cash
    assert rows["orb_v1"].params_json["opening_range_minutes"] == 15
    assert rows["orb_v1"].version == "1.0.0"


def test_reseeding_never_overwrites_edited_params(db: Session) -> None:
    """Approved config changes live in the table; a restart must not revert them to the file."""
    seed_strategies(db, CFG, now=T0)
    db.execute(
        text(
            "update strategies set params_json = '{\"opening_range_minutes\": 30}', version = '1.1.0' where id = 'orb_v1'"
        )
    )
    seed_strategies(db, CFG, now=T0)
    row = db.execute(text("select version, params_json from strategies where id = 'orb_v1'")).one()
    assert (row.version, row.params_json["opening_range_minutes"]) == ("1.1.0", 30)
