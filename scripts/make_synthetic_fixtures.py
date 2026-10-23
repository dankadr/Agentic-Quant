#!/usr/bin/env python3
"""Generate deterministic SYNTHETIC one-minute bars and quotes for replay tests.

The output is a seeded random walk. It is NOT market data and must never be presented as such.
Only integer cent arithmetic is used, so the same arguments always produce identical bytes.

Example:
    make_synthetic_fixtures.py --date 2026-10-19 --symbols SPY,QQQ,AAPL --seed 7 --out DIR
    make_synthetic_fixtures.py ... --drop-bars SPY:14:00-14:05 --quotes-until 15:00   # gap fixture
"""

import argparse
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

START_CENTS = {"SPY": 50000, "QQQ": 43000, "IWM": 21000, "AAPL": 23000, "MSFT": 42000}
OPEN_UTC = (13, 30)  # 09:30 America/New_York in EDT
MINUTES = 390
HEADER_NOTE = "# SYNTHETIC — not market data"


def _fmt(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def _ts(d: date, minute: int, second: int = 0) -> datetime:
    base = datetime(d.year, d.month, d.day, *OPEN_UTC, tzinfo=UTC)
    return base + timedelta(minutes=minute, seconds=second)


def _z(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_hhmm(text: str) -> tuple[int, int]:
    hour, minute = text.split(":")
    return int(hour), int(minute)


def generate(
    day: date,
    symbols: list[str],
    seed: int,
    drop_bars: list[str] | None = None,
    quotes_until: str | None = None,
) -> tuple[str, str]:
    """Return ``(bars_csv, quotes_csv)`` text."""
    rng = random.Random(seed)  # noqa: S311  (deterministic test data, not security)
    drops: dict[str, tuple[datetime, datetime]] = {}
    for spec in drop_bars or []:
        symbol, rest = spec.split(":", 1)
        lo, hi = rest.split("-")
        lo_h, lo_m = _parse_hhmm(lo)
        hi_h, hi_m = _parse_hhmm(hi)
        drops[symbol] = (
            datetime(day.year, day.month, day.day, lo_h, lo_m, tzinfo=UTC),
            datetime(day.year, day.month, day.day, hi_h, hi_m, tzinfo=UTC),
        )
    cutoff = None
    if quotes_until:
        hour, minute = _parse_hhmm(quotes_until)
        cutoff = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)

    close = {s: START_CENTS[s] for s in symbols}
    bars = [HEADER_NOTE, "symbol,ts,open,high,low,close,volume,source_seq"]
    quotes = [HEADER_NOTE, "symbol,ts,bid,ask,bid_size,ask_size"]
    for minute in range(MINUTES):
        start = _ts(day, minute)
        for symbol in symbols:
            open_c = close[symbol]
            close_c = max(100, open_c + rng.randint(-12, 12))
            high_c = max(open_c, close_c) + rng.randint(0, 5)
            low_c = max(1, min(open_c, close_c) - rng.randint(0, 5))
            volume = rng.randint(5000, 50000)
            bps = rng.randint(1, 3)
            spread = max(1, round(close_c * bps / 10000))
            bid_c = close_c - spread // 2
            ask_c = bid_c + spread
            bid_size, ask_size = rng.randint(100, 2000), rng.randint(100, 2000)
            close[symbol] = close_c
            dropped = symbol in drops and drops[symbol][0] <= start < drops[symbol][1]
            if not dropped:
                bars.append(
                    f"{symbol},{_z(start)},{_fmt(open_c)},{_fmt(high_c)},{_fmt(low_c)},"
                    f"{_fmt(close_c)},{volume},{symbol}-{minute}"
                )
            quote_ts = _ts(day, minute, 59)
            if cutoff is None or quote_ts < cutoff:
                quotes.append(
                    f"{symbol},{_z(quote_ts)},{_fmt(bid_c)},{_fmt(ask_c)},{bid_size},{ask_size}"
                )
    return "\n".join(bars) + "\n", "\n".join(quotes) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--drop-bars", action="append", default=[], metavar="SYM:HH:MM-HH:MM")
    parser.add_argument("--quotes-until", metavar="HH:MM")
    args = parser.parse_args()
    bars, quotes = generate(
        args.date, args.symbols.split(","), args.seed, args.drop_bars, args.quotes_until
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bars.csv").write_text(bars)
    (args.out / "quotes.csv").write_text(quotes)


if __name__ == "__main__":
    main()
