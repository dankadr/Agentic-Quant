"""Write provider data as the CSV layout ``CsvReplayProvider`` reads (for backtests and replay)."""

import csv
from datetime import datetime
from pathlib import Path

from trading.core.time import isoformat_z
from trading.data.provider import MarketDataProvider


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
    counts = {"bars": 0, "quotes": 0}
    with (out_dir / "bars.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["symbol", "ts", "open", "high", "low", "close", "volume", "source_seq"])
        for b in sorted(provider.fetch_bars(symbols, start, end), key=lambda r: (r.ts, r.symbol)):
            writer.writerow(
                [
                    b.symbol,
                    isoformat_z(b.ts),
                    b.open,
                    b.high,
                    b.low,
                    b.close,
                    b.volume,
                    b.source_seq or "",
                ]
            )
            counts["bars"] += 1
    if quotes:
        with (out_dir / "quotes.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["symbol", "ts", "bid", "ask", "bid_size", "ask_size"])
            for q in sorted(
                provider.fetch_quotes(symbols, start, end), key=lambda r: (r.ts, r.symbol)
            ):
                writer.writerow(
                    [
                        q.symbol,
                        isoformat_z(q.ts),
                        q.bid,
                        q.ask,
                        q.bid_size if q.bid_size is not None else "",
                        q.ask_size if q.ask_size is not None else "",
                    ]
                )
                counts["quotes"] += 1
    return counts
