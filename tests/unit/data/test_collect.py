from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading.data.calendar import XnysCalendar
from trading.data.collect import collect, merge_days, recent_sessions
from trading.data.models import Bar, Quote
from trading.data.replay_provider import CsvReplayProvider

D = Decimal
CAL = XnysCalendar(date(2026, 9, 1), date(2026, 12, 31))


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.bar_calls: list[date] = []
        self.quote_calls: list[date] = []

    def fetch_bars(self, symbols, start, end):  # type: ignore[no-untyped-def]
        self.bar_calls.append(start.date())
        return [Bar(symbol=s, interval="1m", ts=start, open=D("100"), high=D("101"), low=D("99"),
                    close=D("100.5"), volume=D("500"), provider="fake", source_seq=None) for s in symbols]  # fmt: skip

    def fetch_quotes(self, symbols, start, end):  # type: ignore[no-untyped-def]
        self.quote_calls.append(start.date())
        return [Quote(symbol=s, ts=start + timedelta(seconds=59), bid=D("100"), ask=D("100.02"),
                      bid_size=None, ask_size=None, provider="fake") for s in symbols]  # fmt: skip


def test_recent_sessions_are_completed_trading_days_only() -> None:
    # Sunday 25 Oct 2026: the last completed session is Friday 23rd; weekends are skipped
    days = recent_sessions(CAL, as_of=datetime(2026, 10, 25, 12, tzinfo=UTC), count=3)
    assert days == [date(2026, 10, 21), date(2026, 10, 22), date(2026, 10, 23)]


def test_today_is_excluded_until_the_session_has_closed() -> None:
    during = recent_sessions(CAL, as_of=datetime(2026, 10, 23, 15, tzinfo=UTC), count=1)
    after = recent_sessions(CAL, as_of=datetime(2026, 10, 23, 21, tzinfo=UTC), count=1)
    assert during == [date(2026, 10, 22)]
    assert after == [date(2026, 10, 23)]


def test_collect_writes_per_day_files_and_is_resumable(tmp_path: Path) -> None:
    provider = FakeProvider()
    days = [date(2026, 10, 22), date(2026, 10, 23)]
    first = collect(provider, ["SPY"], CAL, days, tmp_path)  # type: ignore[arg-type]
    assert first == {"bars_days": 2, "quote_days": 2, "skipped": 0}
    for d in days:
        assert (tmp_path / d.isoformat() / "bars.csv").exists()
        assert (tmp_path / d.isoformat() / "quotes.csv").exists()
    provider2 = FakeProvider()
    again = collect(provider2, ["SPY"], CAL, days, tmp_path)  # type: ignore[arg-type]
    assert again == {"bars_days": 0, "quote_days": 0, "skipped": 4}
    assert provider2.bar_calls == [] and provider2.quote_calls == []


def test_a_partial_day_is_redone(tmp_path: Path) -> None:
    provider = FakeProvider()
    d = date(2026, 10, 23)
    collect(provider, ["SPY"], CAL, [d], tmp_path)  # type: ignore[arg-type]
    (tmp_path / d.isoformat() / "quotes.csv").unlink()
    provider2 = FakeProvider()
    result = collect(provider2, ["SPY"], CAL, [d], tmp_path)  # type: ignore[arg-type]
    assert result == {"bars_days": 0, "quote_days": 1, "skipped": 1}


def test_bars_are_collected_for_every_day_before_any_quotes(tmp_path: Path) -> None:
    order: list[str] = []

    class Spy(FakeProvider):
        def fetch_bars(self, symbols, start, end):  # type: ignore[no-untyped-def]
            order.append("bars")
            return super().fetch_bars(symbols, start, end)

        def fetch_quotes(self, symbols, start, end):  # type: ignore[no-untyped-def]
            order.append("quotes")
            return super().fetch_quotes(symbols, start, end)

    collect(Spy(), ["SPY"], CAL, [date(2026, 10, 22), date(2026, 10, 23)], tmp_path)  # type: ignore[arg-type]
    assert order == ["bars", "bars", "quotes", "quotes"]


def test_merged_days_load_through_the_replay_provider(tmp_path: Path) -> None:
    days = [date(2026, 10, 22), date(2026, 10, 23)]
    collect(FakeProvider(), ["SPY"], CAL, days, tmp_path / "raw")  # type: ignore[arg-type]
    merged = tmp_path / "merged"
    counts = merge_days(tmp_path / "raw", days, merged)
    assert counts == {"days": 2, "bars": 2, "quotes": 2}
    provider = CsvReplayProvider(merged)
    start = datetime(2026, 10, 22, tzinfo=UTC)
    end = datetime(2026, 10, 24, tzinfo=UTC)
    assert len(list(provider.fetch_bars(["SPY"], start, end))) == 2
    assert len(list(provider.fetch_quotes(["SPY"], start, end))) == 2
    assert (merged / "bars.csv").read_text().count("symbol,ts") == 1  # one header
