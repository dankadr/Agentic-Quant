from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading.data.models import Bar, Quote
from trading.data.provider import DataFormatError
from trading.data.replay_provider import CsvReplayProvider

FIXTURE = Path("tests/fixtures/replay/2026-10-19")
DAY_START = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
DAY_END = datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
HEADER = "symbol,ts,open,high,low,close,volume,source_seq\n"


def _write(tmp_path: Path, name: str, text: str) -> CsvReplayProvider:
    (tmp_path / name).write_text(text)
    return CsvReplayProvider(tmp_path)


def test_reads_fixture_bars_and_quotes() -> None:
    p = CsvReplayProvider(FIXTURE)
    bars = list(p.fetch_bars(["SPY", "QQQ", "AAPL"], DAY_START, DAY_END))
    quotes = list(p.fetch_quotes(["SPY", "QQQ", "AAPL"], DAY_START, DAY_END))
    assert len(bars) == 1170
    assert len(quotes) == 1170
    first = bars[0]
    assert first == Bar(
        symbol="SPY", interval="1m", ts=DAY_START, open=Decimal("500.00"), high=Decimal("500.01"),
        low=Decimal("499.95"), close=Decimal("499.98"), volume=Decimal(47659), provider="replay",
        source_seq="SPY-0",
    )  # fmt: skip
    assert isinstance(first.open, Decimal)
    assert quotes[0].ts == datetime(2026, 10, 19, 13, 30, 59, tzinfo=UTC)
    assert quotes[0].spread_bps > 0


def test_symbol_and_half_open_window_filters() -> None:
    p = CsvReplayProvider(FIXTURE)
    end = datetime(2026, 10, 19, 13, 32, tzinfo=UTC)
    bars = list(p.fetch_bars(["SPY"], DAY_START, end))
    assert [b.ts.minute for b in bars] == [30, 31]
    assert {b.symbol for b in bars} == {"SPY"}


def test_preserves_file_order(tmp_path: Path) -> None:
    p = _write(
        tmp_path, "bars.csv",
        HEADER + "SPY,2026-10-19T13:31:00Z,1,1,1,1,1,a\nSPY,2026-10-19T13:30:00Z,1,1,1,1,1,b\n",
    )  # fmt: skip
    assert [b.source_seq for b in p.fetch_bars(["SPY"], DAY_START, DAY_END)] == ["a", "b"]


def test_comments_and_blank_lines_skipped(tmp_path: Path) -> None:
    p = _write(
        tmp_path, "bars.csv",
        "# SYNTHETIC\n" + HEADER + "\nSPY,2026-10-19T13:30:00Z,1,1,1,1,1,a\n",
    )  # fmt: skip
    assert len(list(p.fetch_bars(["SPY"], DAY_START, DAY_END))) == 1


def test_csv_naive_timestamp_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "bars.csv", HEADER + "SPY,2026-10-19 09:31:00,1,1,1,1,1,a\n")
    with pytest.raises(DataFormatError, match=r"bars\.csv:2"):
        list(p.fetch_bars(["SPY"], DAY_START, DAY_END))


def test_offset_timestamps_are_converted_to_utc(tmp_path: Path) -> None:
    p = _write(tmp_path, "bars.csv", HEADER + "SPY,2026-10-19T09:30:00-04:00,1,1,1,1,1,a\n")
    (bar,) = p.fetch_bars(["SPY"], DAY_START, DAY_END)
    assert bar.ts == DAY_START


def test_bad_decimal_and_missing_column_rejected(tmp_path: Path) -> None:
    p = _write(tmp_path, "bars.csv", HEADER + "SPY,2026-10-19T13:30:00Z,abc,1,1,1,1,a\n")
    with pytest.raises(DataFormatError, match=r"bars\.csv:2"):
        list(p.fetch_bars(["SPY"], DAY_START, DAY_END))
    q = _write(tmp_path, "bars.csv", "symbol,ts,open\nSPY,2026-10-19T13:30:00Z,1\n")
    with pytest.raises(DataFormatError, match="missing column"):
        list(q.fetch_bars(["SPY"], DAY_START, DAY_END))


def test_missing_files_yield_nothing(tmp_path: Path) -> None:
    p = CsvReplayProvider(tmp_path)
    assert list(p.fetch_bars(["SPY"], DAY_START, DAY_END)) == []
    assert list(p.fetch_quotes(["SPY"], DAY_START, DAY_END)) == []
    assert list(p.fetch_corporate_actions(["SPY"], date(2026, 1, 1), date(2026, 12, 31))) == []


def test_corporate_actions_parsed_and_filtered(tmp_path: Path) -> None:
    p = _write(
        tmp_path, "corporate_actions.csv",
        "symbol,action_type,ex_date,ratio_from,ratio_to,cash_amount,announced_at\n"
        "SPY,SPLIT,2026-10-20,1,2,,2026-10-01T00:00:00Z\n"
        "SPY,CASH_DIVIDEND,2026-10-21,,,0.50,2026-10-01T00:00:00Z\n"
        "QQQ,SPLIT,2026-10-20,1,2,,2026-10-01T00:00:00Z\n",
    )  # fmt: skip
    actions = list(p.fetch_corporate_actions(["SPY"], date(2026, 10, 20), date(2026, 10, 20)))
    assert [(a.action_type, a.ratio_to, a.cash_amount) for a in actions] == [
        ("SPLIT", Decimal(2), None)
    ]


def test_bar_and_quote_derived_values() -> None:
    bar = Bar(symbol="SPY", interval="1m", ts=DAY_START, open=Decimal(1), high=Decimal(1),
              low=Decimal(1), close=Decimal(1), volume=Decimal(1), provider="p", source_seq=None)  # fmt: skip
    assert bar.available_at == datetime(2026, 10, 19, 13, 31, tzinfo=UTC)
    quote = Quote(symbol="SPY", ts=DAY_START, bid=Decimal("99.99"), ask=Decimal("100.01"),
                  bid_size=None, ask_size=None, provider="p")  # fmt: skip
    assert quote.mid == Decimal("100.00")
    assert quote.spread_bps == Decimal("2")
