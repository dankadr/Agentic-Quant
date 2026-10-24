from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session

from trading.core.config import load_config
from trading.data.instruments import upsert_universe
from trading.db.models.market import MarketBar
from trading.quant.bars import load_session_bars

UNIVERSE = load_config(Path("configs")).universe
DAY = date(2026, 10, 19)


def t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 10, 19, h, m, s, tzinfo=UTC)


def add(
    db: Session,
    iid,
    ts: datetime,
    *,
    close="100",
    flags=(),
    ingested=None,
    day=DAY,
    provider="replay",
) -> None:  # type: ignore[no-untyped-def]
    db.add(MarketBar(instrument_id=iid, interval="1m", ts=ts, provider=provider, open=Decimal(close), high=Decimal(close),
                     low=Decimal(close), close=Decimal(close), volume=Decimal(1000),
                     ingested_at=ingested or ts + timedelta(minutes=1), session_date=day, source_seq=None,
                     quality_flags=list(flags)))  # fmt: skip
    db.flush()


def load(db: Session, as_of: datetime, symbols=("SPY",)):  # type: ignore[no-untyped-def]
    return load_session_bars(
        db, symbols=list(symbols), session_date=DAY, as_of=as_of, provider="replay"
    )


def test_only_closed_bars_are_visible_look_ahead_boundary(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    for minute in (30, 31, 32):
        add(db, spy.id, t(13, minute))
    assert [b.ts.minute for b in load(db, t(13, 32, 59))["SPY"]] == [
        30,
        31,
    ]  # 13:32 bar is still forming
    assert [b.ts.minute for b in load(db, t(13, 33, 0))["SPY"]] == [30, 31, 32]


def test_late_ingested_bars_are_invisible(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    add(db, spy.id, t(13, 30), ingested=t(14, 0))
    assert load(db, t(13, 45))["SPY"] == ()
    assert len(load(db, t(14, 0))["SPY"]) == 1


def test_corrupt_bars_are_excluded_but_informational_flags_kept(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    add(db, spy.id, t(13, 30), flags=("NON_POSITIVE_PRICE",))
    add(db, spy.id, t(13, 31), flags=("GAP_BEFORE",))
    add(db, spy.id, t(13, 32), flags=("OHLC_INCONSISTENT",))
    assert [b.ts.minute for b in load(db, t(14, 0))["SPY"]] == [31]


def test_gaps_are_not_filled_and_order_is_oldest_first(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    for minute in (35, 30, 40):
        add(db, spy.id, t(13, minute))
    assert [b.ts.minute for b in load(db, t(14, 0))["SPY"]] == [30, 35, 40]


def test_other_sessions_and_providers_and_symbols_are_separate(db: Session) -> None:
    ids = upsert_universe(db, UNIVERSE)
    add(db, ids["SPY"].id, t(13, 30), day=date(2026, 10, 16))
    add(db, ids["SPY"].id, t(13, 31), provider="other")
    add(db, ids["QQQ"].id, t(13, 32))
    result = load(db, t(14, 0), ("SPY", "QQQ", "IWM"))
    assert result["SPY"] == ()
    assert len(result["QQQ"]) == 1
    assert result["IWM"] == ()
    assert result["QQQ"][0].symbol == "QQQ"
    assert result["QQQ"][0].close == Decimal("100")
