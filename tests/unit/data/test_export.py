from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from trading.data.export import export_csv
from trading.data.models import Bar, Quote
from trading.data.replay_provider import CsvReplayProvider

D = Decimal
T = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


class Fake:
    name = "fake"

    def fetch_bars(self, symbols, start, end):  # type: ignore[no-untyped-def]
        return [
            Bar(symbol="SPY", interval="1m", ts=T, open=D("100.1"), high=D("100.25"), low=D("99.9"),
                close=D("100.2"), volume=D("1200"), provider="fake", source_seq=None)
        ]  # fmt: skip

    def fetch_quotes(self, symbols, start, end):  # type: ignore[no-untyped-def]
        return [Quote(symbol="SPY", ts=T.replace(second=59), bid=D("100.19"), ask=D("100.21"),
                      bid_size=D(2), ask_size=None, provider="fake")]  # fmt: skip


def test_exported_csv_round_trips_through_the_replay_provider(tmp_path: Path) -> None:
    counts = export_csv(Fake(), ["SPY"], T, T.replace(hour=20), tmp_path)  # type: ignore[arg-type]
    assert counts == {"bars": 1, "quotes": 1}
    replay = CsvReplayProvider(tmp_path)
    (bar,) = replay.fetch_bars(["SPY"], T, T.replace(hour=20))
    assert (bar.open, bar.high, bar.close, bar.volume, bar.ts) == (
        D("100.1"),
        D("100.25"),
        D("100.2"),
        D("1200"),
        T,
    )
    (quote,) = replay.fetch_quotes(["SPY"], T, T.replace(hour=20))
    assert (quote.bid, quote.ask, quote.bid_size, quote.ask_size) == (
        D("100.19"),
        D("100.21"),
        D(2),
        None,
    )


def test_quotes_can_be_skipped(tmp_path: Path) -> None:
    counts = export_csv(Fake(), ["SPY"], T, T.replace(hour=20), tmp_path, quotes=False)  # type: ignore[arg-type]
    assert counts == {"bars": 1, "quotes": 0}
    assert not (tmp_path / "quotes.csv").exists()
