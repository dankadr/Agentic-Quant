from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.data.calendar import MarketCalendar
from trading.data.ingest import ingest_bars, ingest_corporate_actions, ingest_quotes
from trading.data.instruments import upsert_universe
from trading.data.models import QualityFlag
from trading.data.replay_provider import CsvReplayProvider

FIXTURES = Path("tests/fixtures/replay")
START = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
END = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
SYMBOLS = ["SPY", "QQQ", "AAPL"]
HEADER = "symbol,ts,open,high,low,close,volume,source_seq\n"


def _setup(db: Session) -> None:
    upsert_universe(db, load_config(Path("configs")).universe)


def _provider(tmp_path: Path, bars: str) -> CsvReplayProvider:
    (tmp_path / "bars.csv").write_text(HEADER + bars)
    return CsvReplayProvider(tmp_path)


def _bar_rows(db: Session, symbol: str = "SPY") -> list[tuple[datetime, list[str], date | None]]:
    rows = db.execute(
        text(
            "select b.ts, b.quality_flags, b.session_date from market_bars b"
            " join instruments i on i.id = b.instrument_id where i.symbol = :s order by b.ts"
        ),
        {"s": symbol},
    ).all()
    return [(r[0], list(r[1]), r[2]) for r in rows]


def test_ingest_inserts_and_is_idempotent(
    db: Session, clock: ManualClock, calendar: MarketCalendar
) -> None:
    _setup(db)
    p = CsvReplayProvider(FIXTURES / "2026-10-19")
    first = ingest_bars(
        db, p, symbols=SYMBOLS, start=START, end=END, calendar=calendar, clock=clock
    )
    assert (first.inserted, first.duplicates, first.gaps) == (1170, 0, [])
    assert first.flag_counts == {}
    second = ingest_bars(
        db, p, symbols=SYMBOLS, start=START, end=END, calendar=calendar, clock=clock
    )
    assert (second.inserted, second.duplicates) == (0, 1170)


def test_ingest_stamps_clock_and_session_date(
    db: Session, clock: ManualClock, calendar: MarketCalendar
) -> None:
    _setup(db)
    ingest_bars(db, CsvReplayProvider(FIXTURES / "2026-10-19"), symbols=["SPY"], start=START,
                end=START + timedelta(minutes=1), calendar=calendar, clock=clock)  # fmt: skip
    row = db.execute(
        text("select ingested_at, session_date, provider, interval from market_bars")
    ).one()
    assert row.ingested_at == clock.now()
    assert row.session_date == date(2026, 10, 19)
    assert (row.provider, row.interval) == ("replay", "1m")


def test_gap_detected_not_filled(db: Session, clock: ManualClock, calendar: MarketCalendar) -> None:
    _setup(db)
    report = ingest_bars(db, CsvReplayProvider(FIXTURES / "gaps"), symbols=SYMBOLS, start=START,
                         end=END, calendar=calendar, clock=clock)  # fmt: skip
    assert report.gaps == [
        (
            "SPY",
            datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
            datetime(2026, 10, 19, 14, 5, tzinfo=UTC),
        )
    ]
    assert report.flag_counts == {QualityFlag.GAP_BEFORE: 1}
    spy = _bar_rows(db)
    assert len(spy) == 385
    assert not any(
        datetime(2026, 10, 19, 14, 0, tzinfo=UTC) <= ts < datetime(2026, 10, 19, 14, 5, tzinfo=UTC)
        for ts, _, _ in spy
    )
    flagged = [ts for ts, flags, _ in spy if "GAP_BEFORE" in flags]
    assert flagged == [datetime(2026, 10, 19, 14, 5, tzinfo=UTC)]


def test_gap_detected_across_separate_ingest_runs(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    a = tmp_path / "a"
    a.mkdir()
    b = tmp_path / "b"
    b.mkdir()
    pa = _provider(a, "SPY,2026-10-19T13:30:00Z,1,1,1,1,1,x\n")
    pb = _provider(b, "SPY,2026-10-19T13:35:00Z,1,1,1,1,1,y\n")
    ingest_bars(db, pa, symbols=["SPY"], start=START, end=END, calendar=calendar, clock=clock)
    report = ingest_bars(
        db, pb, symbols=["SPY"], start=START, end=END, calendar=calendar, clock=clock
    )
    assert [(g[0], g[1].minute, g[2].minute) for g in report.gaps] == [("SPY", 31, 35)]


def test_non_positive_inconsistent_and_zero_volume_flagged(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    p = _provider(
        tmp_path,
        "SPY,2026-10-19T13:30:00Z,0,1,1,1,5,a\n"  # zero price
        "SPY,2026-10-19T13:31:00Z,10,9,11,10,5,b\n"  # high < low
        "SPY,2026-10-19T13:32:00Z,10,12,9,11,0,c\n",  # zero volume only
    )
    report = ingest_bars(
        db, p, symbols=["SPY"], start=START, end=END, calendar=calendar, clock=clock
    )
    flags = [set(f) for _, f, _ in _bar_rows(db)]
    assert flags == [{"NON_POSITIVE_PRICE"}, {"OHLC_INCONSISTENT"}, {"ZERO_VOLUME"}]
    assert report.flag_counts[QualityFlag.NON_POSITIVE_PRICE] == 1


def test_outside_session_flagged(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    p = _provider(
        tmp_path,
        "SPY,2026-10-19T21:00:00Z,1,1,1,1,1,a\n"  # after the close
        "SPY,2026-10-24T15:00:00Z,1,1,1,1,1,b\n",  # a Saturday
    )
    ingest_bars(db, p, symbols=["SPY"], start=START, end=datetime(2026, 10, 25, tzinfo=UTC),
                calendar=calendar, clock=clock)  # fmt: skip
    rows = _bar_rows(db)
    assert [(set(f), sd) for _, f, sd in rows] == [({"OUTSIDE_SESSION"}, None)] * 2


def test_out_of_order_flagged(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    p = _provider(
        tmp_path,
        "SPY,2026-10-19T13:31:00Z,1,1,1,1,1,a\nSPY,2026-10-19T13:30:00Z,1,1,1,1,1,b\n",
    )
    ingest_bars(db, p, symbols=["SPY"], start=START, end=END, calendar=calendar, clock=clock)
    by_ts = {ts.minute: set(f) for ts, f, _ in _bar_rows(db)}
    assert by_ts == {30: {"OUT_OF_ORDER"}, 31: set()}


def test_conflicting_duplicate_flags_existing_and_keeps_original(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    first = tmp_path / "one"
    first.mkdir()
    second = tmp_path / "two"
    second.mkdir()
    ingest_bars(db, _provider(first, "SPY,2026-10-19T13:30:00Z,1,2,1,2,5,a\n"), symbols=["SPY"],
                start=START, end=END, calendar=calendar, clock=clock)  # fmt: skip
    report = ingest_bars(db, _provider(second, "SPY,2026-10-19T13:30:00Z,1,2,1,3,5,b\n"),
                         symbols=["SPY"], start=START, end=END, calendar=calendar, clock=clock)  # fmt: skip
    assert (report.inserted, report.duplicates) == (0, 0)
    assert report.flag_counts == {QualityFlag.DUPLICATE_CONFLICT: 1}
    assert db.execute(text("select close from market_bars")).scalar_one() == Decimal("2")
    assert _bar_rows(db)[0][1] == ["DUPLICATE_CONFLICT"]


def test_unknown_symbol_reported_not_inserted(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    p = _provider(tmp_path, "ZZZZ,2026-10-19T13:30:00Z,1,1,1,1,1,a\n")
    report = ingest_bars(
        db, p, symbols=["ZZZZ"], start=START, end=END, calendar=calendar, clock=clock
    )
    assert report.unknown_symbols == {"ZZZZ"}
    assert report.inserted == 0
    assert db.execute(text("select count(*) from market_bars")).scalar_one() == 0


def test_quotes_ingested_idempotent_and_crossed_flagged(
    db: Session, clock: ManualClock, calendar: MarketCalendar, tmp_path: Path
) -> None:
    _setup(db)
    p = CsvReplayProvider(FIXTURES / "2026-10-19")
    r1 = ingest_quotes(db, p, symbols=SYMBOLS, start=START, end=END, calendar=calendar, clock=clock)
    r2 = ingest_quotes(db, p, symbols=SYMBOLS, start=START, end=END, calendar=calendar, clock=clock)
    assert (r1.inserted, r2.inserted, r2.duplicates) == (1170, 0, 1170)
    d = tmp_path / "q"
    d.mkdir()
    (d / "quotes.csv").write_text(
        "symbol,ts,bid,ask,bid_size,ask_size\n"
        "IWM,2026-10-19T14:00:00Z,100.05,100.00,1,1\n"
        "IWM,2026-10-19T14:01:00Z,0,100.00,1,1\n"
    )
    ingest_quotes(
        db,
        CsvReplayProvider(d),
        symbols=["IWM"],
        start=START,
        end=END,
        calendar=calendar,
        clock=clock,
    )
    flags = (
        db.execute(
            text(
                "select quality_flags from market_quotes q join instruments i on i.id=q.instrument_id"
                " where i.symbol='IWM' order by q.ts"
            )
        )
        .scalars()
        .all()
    )
    assert [set(f) for f in flags] == [{"OHLC_INCONSISTENT"}, {"NON_POSITIVE_PRICE"}]


def test_corporate_actions_ingested_once(db: Session, clock: ManualClock, tmp_path: Path) -> None:
    _setup(db)
    (tmp_path / "corporate_actions.csv").write_text(
        "symbol,action_type,ex_date,ratio_from,ratio_to,cash_amount,announced_at\n"
        "SPY,SPLIT,2026-10-20,1,2,,2026-10-01T00:00:00Z\n"
    )
    p = CsvReplayProvider(tmp_path)
    r1 = ingest_corporate_actions(
        db, p, symbols=["SPY"], start=date(2026, 10, 1), end=date(2026, 10, 31), clock=clock
    )
    r2 = ingest_corporate_actions(
        db, p, symbols=["SPY"], start=date(2026, 10, 1), end=date(2026, 10, 31), clock=clock
    )
    assert (r1.inserted, r2.inserted, r2.duplicates) == (1, 0, 1)
