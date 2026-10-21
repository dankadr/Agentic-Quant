from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.config import load_config
from trading.core.enums import AssetType
from trading.data.instruments import load_instruments, qty_increment, upsert_universe

CFG = load_config(Path("configs"))


def test_upsert_universe_idempotent(db: Session) -> None:
    first = upsert_universe(db, CFG.universe)
    second = upsert_universe(db, CFG.universe)
    assert set(first) == {"SPY", "QQQ", "IWM", "AAPL", "MSFT"}
    assert {s: i.id for s, i in first.items()} == {s: i.id for s, i in second.items()}
    assert db.execute(text("select count(*) from instruments")).scalar_one() == 5


def test_instrument_fields(db: Session) -> None:
    spy = upsert_universe(db, CFG.universe)["SPY"]
    assert spy.asset_type is AssetType.ETF
    assert spy.tick_size == Decimal("0.01")
    assert spy.lot_size == Decimal(1)
    assert spy.fractional_eligible is True
    assert spy.sector == "Broad Market"


def test_upsert_updates_changed_attributes(db: Session) -> None:
    upsert_universe(db, CFG.universe)
    changed = [
        u.model_copy(update={"fractional_eligible": False, "sector": "Other"})
        if u.symbol == "AAPL"
        else u
        for u in CFG.universe
    ]
    aapl = upsert_universe(db, changed)["AAPL"]
    assert (aapl.fractional_eligible, aapl.sector) == (False, "Other")


def test_load_instruments_only_active(db: Session) -> None:
    upsert_universe(db, CFG.universe)
    db.execute(text("update instruments set active = false where symbol = 'IWM'"))
    assert "IWM" not in load_instruments(db)
    assert len(load_instruments(db)) == 4


def test_qty_increment_whole_vs_fractional(db: Session) -> None:
    broker = CFG.paper_broker
    inst = upsert_universe(db, CFG.universe)["SPY"]
    assert qty_increment(inst, broker) == Decimal("0.0001")
    whole = inst.__class__(**{**inst.__dict__, "fractional_eligible": False})
    assert qty_increment(whole, broker) == Decimal(1)
    broker_whole = broker.model_copy(update={"fractional_shares": False})
    assert qty_increment(inst, broker_whole) == Decimal(1)
