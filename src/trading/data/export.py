"""Write provider data as the CSV layout ``CsvReplayProvider`` reads (for backtests and replay)."""

import csv
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from trading.core.time import isoformat_z
from trading.data.models import Bar, Quote
from trading.data.provider import MarketDataProvider


def _atomic(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def write_bars(path: Path, bars: Iterable[Bar]) -> int:
    """Write bars atomically (a crash never leaves a half-written file under the final name)."""
    count = 0
    tmp = _atomic(path)
    with tmp.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "ts", "open", "high", "low", "close", "volume", "source_seq"])
        for b in sorted(bars, key=lambda r: (r.ts, r.symbol)):
            writer.writerow(
                [
                    b.symbol, isoformat_z(b.ts), b.open, b.high, b.low, b.close, b.volume,
                    b.source_seq or "",
                ]
            )  # fmt: skip
            count += 1
    os.replace(tmp, path)
    return count


def write_quotes(path: Path, quotes: Iterable[Quote]) -> int:
    count = 0
    tmp = _atomic(path)
    with tmp.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "ts", "bid", "ask", "bid_size", "ask_size"])
        for q in sorted(quotes, key=lambda r: (r.ts, r.symbol)):
            writer.writerow(
                [
                    q.symbol, isoformat_z(q.ts), q.bid, q.ask,
                    "" if q.bid_size is None else q.bid_size,
                    "" if q.ask_size is None else q.ask_size,
                ]
            )  # fmt: skip
            count += 1
    os.replace(tmp, path)
    return count


def export_csv(
    provider: MarketDataProvider,
    symbols: list[str],
    start: datetime,
    end: datetime,
    out_dir: Path,
    *,
    quotes: bool = True,
) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {
        "bars": write_bars(out_dir / "bars.csv", provider.fetch_bars(symbols, start, end)),
        "quotes": 0,
    }
    if quotes:
        counts["quotes"] = write_quotes(
            out_dir / "quotes.csv", provider.fetch_quotes(symbols, start, end)
        )
    return counts
