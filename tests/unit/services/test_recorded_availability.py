from datetime import UTC, datetime
from pathlib import Path

from trading.data.replay_provider import CsvReplayProvider


def test_recorded_late_bar_is_not_visible_before_ingestion(tmp_path: Path):
    (tmp_path / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume,available_at,quality_flags\nSPY,2026-10-19T13:30:00Z,10,10,10,10,100,2026-10-19T14:00:00Z,\n"
    )
    p = CsvReplayProvider(tmp_path)
    assert (
        list(
            p.fetch_bars(
                ["SPY"],
                datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
                datetime(2026, 10, 19, 13, 31, tzinfo=UTC),
            )
        )
        == []
    )


def test_normal_ingestion_after_bar_end_is_visible_at_replay_tick(tmp_path: Path):
    (tmp_path / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume,available_at\nSPY,2026-10-19T13:30:00Z,10,10,10,10,100,2026-10-19T13:31:05Z\n"
    )
    p = CsvReplayProvider(
        tmp_path, availability_clock=lambda: datetime(2026, 10, 19, 13, 31, 5, tzinfo=UTC)
    )
    assert (
        len(
            list(
                p.fetch_bars(
                    ["SPY"],
                    datetime(2026, 10, 19, 13, 30, tzinfo=UTC),
                    datetime(2026, 10, 19, 13, 31, tzinfo=UTC),
                )
            )
        )
        == 1
    )


def test_known_bad_latest_bar_retains_its_flag_instead_of_disappearing(tmp_path: Path):
    (tmp_path / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume,quality_flags\nSPY,2026-10-19T13:30:00Z,10,10,10,10,100,DUPLICATE_CONFLICT\n"
    )
    p = CsvReplayProvider(tmp_path)
    start = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
    assert len(list(p.fetch_bars(["SPY"], start, datetime(2026, 10, 19, 13, 31, tzinfo=UTC)))) == 1
    assert "DUPLICATE_CONFLICT" in p.recorded_flags("bars.csv", "SPY", start)
