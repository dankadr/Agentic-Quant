from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.config import load_config
from trading.data.instruments import upsert_universe
from trading.data.snapshot import MarketSnapshot, build_snapshot, load_snapshot
from trading.db.models.market import MarketBar, MarketQuote

UNIVERSE = load_config(Path("configs")).universe
PROVIDER = "replay"


def _t(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 10, 19, hh, mm, ss, tzinfo=UTC)


def _add_bar(
    db: Session,
    instrument_id: UUID,
    ts: datetime,
    *,
    close: str = "100.00",
    ingested_at: datetime | None = None,
    flags: tuple[str, ...] = (),
) -> None:
    db.add(
        MarketBar(
            instrument_id=instrument_id, interval="1m", ts=ts, provider=PROVIDER,
            open=Decimal(close), high=Decimal(close), low=Decimal(close), close=Decimal(close),
            volume=Decimal(1000), ingested_at=ingested_at or ts + timedelta(minutes=1),
            session_date=ts.date(), source_seq=None, quality_flags=list(flags),
        )
    )  # fmt: skip
    db.flush()


def _add_quote(
    db: Session,
    instrument_id: UUID,
    ts: datetime,
    *,
    bid: str = "99.99",
    ask: str = "100.01",
    ingested_at: datetime | None = None,
) -> None:
    db.add(
        MarketQuote(
            instrument_id=instrument_id, ts=ts, provider=PROVIDER, bid=Decimal(bid), ask=Decimal(ask),
            bid_size=Decimal(100), ask_size=Decimal(100), ingested_at=ingested_at or ts,
            quality_flags=[],
        )
    )  # fmt: skip
    db.flush()


def _snapshot(db: Session, as_of: datetime, symbols: list[str] | None = None) -> MarketSnapshot:
    return build_snapshot(
        db,
        symbols=["SPY"] if symbols is None else symbols,
        as_of=as_of,
        provider=PROVIDER,
        max_bar_age_seconds=90,
    )


def test_bar_not_visible_until_closed(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 44), close="100.00")
    _add_bar(db, spy.id, _t(13, 45), close="101.00")
    before_close = _snapshot(db, _t(13, 45, 59))
    after_close = _snapshot(db, _t(13, 46, 0))
    assert before_close.symbols["SPY"].last_bar is not None
    assert before_close.symbols["SPY"].last_bar.ts == _t(13, 44)
    assert after_close.symbols["SPY"].last_bar is not None
    assert after_close.symbols["SPY"].last_bar.ts == _t(13, 45)
    assert after_close.symbols["SPY"].last_bar.close == Decimal("101.00")


def test_late_ingested_data_is_invisible(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 40), ingested_at=_t(14, 10))
    _add_quote(db, spy.id, _t(13, 40, 30), ingested_at=_t(14, 10))
    snap = _snapshot(db, _t(14, 0))
    assert snap.symbols["SPY"].last_bar is None
    assert snap.symbols["SPY"].quote is None
    assert snap.symbols["SPY"].stale is True
    later = _snapshot(db, _t(14, 10))
    assert later.symbols["SPY"].last_bar is not None


def test_future_quote_invisible_and_latest_quote_used(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 45))
    _add_quote(db, spy.id, _t(13, 46, 10), bid="99.50", ask="99.60")
    _add_quote(db, spy.id, _t(13, 46, 40), bid="99.70", ask="99.80")
    snap = _snapshot(db, _t(13, 46, 30))
    assert snap.symbols["SPY"].quote is not None
    assert snap.symbols["SPY"].quote.bid == Decimal("99.50")


def test_stale_boundary_90_seconds(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 45))  # available 13:46:00
    fresh = _snapshot(db, _t(13, 47, 30)).symbols["SPY"]
    stale = _snapshot(db, _t(13, 47, 31)).symbols["SPY"]
    assert (fresh.bar_age_seconds, fresh.stale, fresh.tradable) == (90, False, True)
    assert (stale.bar_age_seconds, stale.stale, stale.tradable) == (91, True, False)


@pytest.mark.parametrize(
    "flag", ["NON_POSITIVE_PRICE", "OHLC_INCONSISTENT", "OUTSIDE_SESSION", "DUPLICATE_CONFLICT"]
)
def test_blocking_flag_not_tradable(db: Session, flag: str) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 45), flags=(flag,))
    view = _snapshot(db, _t(13, 46, 10)).symbols["SPY"]
    assert (view.stale, view.tradable) == (False, False)
    assert view.bar_flags == (flag,)


@pytest.mark.parametrize("flag", ["GAP_BEFORE", "ZERO_VOLUME", "OUT_OF_ORDER"])
def test_informational_flag_still_tradable(db: Session, flag: str) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 45), flags=(flag,))
    assert _snapshot(db, _t(13, 46, 10)).symbols["SPY"].tradable is True


def test_quality_status_ok_degraded_stale(db: Session) -> None:
    ids = upsert_universe(db, UNIVERSE)
    _add_bar(db, ids["SPY"].id, _t(13, 45))
    _add_bar(db, ids["QQQ"].id, _t(13, 45))
    as_of = _t(13, 46, 10)
    assert _snapshot(db, as_of, ["SPY", "QQQ"]).quality_status == "OK"
    assert _snapshot(db, as_of, ["SPY", "IWM"]).quality_status == "DEGRADED"
    assert _snapshot(db, as_of, ["IWM", "AAPL"]).quality_status == "STALE"
    assert _snapshot(db, as_of, []).quality_status == "STALE"


def test_unknown_symbol_raises(db: Session) -> None:
    upsert_universe(db, UNIVERSE)
    with pytest.raises(ValueError, match="unknown instrument"):
        _snapshot(db, _t(14, 0), ["NOPE"])


def test_persisted_hash_deterministic_and_roundtrips(db: Session) -> None:
    spy = upsert_universe(db, UNIVERSE)["SPY"]
    _add_bar(db, spy.id, _t(13, 45), close="100.50")
    _add_quote(db, spy.id, _t(13, 45, 59))
    a = _snapshot(db, _t(13, 46, 10))
    b = _snapshot(db, _t(13, 46, 10))
    assert a.id != b.id
    assert a.data_hash == b.data_hash
    assert _snapshot(db, _t(13, 46, 11)).data_hash != a.data_hash
    assert load_snapshot(db, a.id) == a
    stored = db.execute(
        text("select data_hash, quality_status from market_snapshots where id = :i"), {"i": a.id}
    ).one()
    assert tuple(stored) == (a.data_hash, "OK")


def test_hash_does_not_depend_on_database_ids(db: Session) -> None:
    def build_in_savepoint() -> str:
        with db.begin_nested() as sp:
            spy = upsert_universe(db, UNIVERSE)["SPY"]
            _add_bar(db, spy.id, _t(13, 45), close="100.50")
            _add_quote(db, spy.id, _t(13, 45, 59))
            digest = _snapshot(db, _t(13, 46, 10)).data_hash
            sp.rollback()
        return digest

    assert build_in_savepoint() == build_in_savepoint()
