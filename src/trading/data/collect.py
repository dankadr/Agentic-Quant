"""Long-running, resumable collection of historical sessions for backtests and research.

One directory per session (``<root>/YYYY-MM-DD/bars.csv`` and ``quotes.csv``). Files are written
atomically, so a file that exists is complete; re-running skips what is already there. Bars for
every day come first (cheap), then quotes newest-first (one request per symbol-minute, slow), so
stopping early still leaves usable days.
"""

import shutil
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from trading.data.calendar import MarketCalendar
from trading.data.export import write_bars, write_quotes
from trading.data.provider import MarketDataProvider


def recent_sessions(calendar: MarketCalendar, *, as_of: datetime, count: int) -> list[date]:
    """The last ``count`` sessions that have fully closed by ``as_of``, oldest first."""
    found: list[date] = []
    day = as_of.date()
    for _ in range(count * 3 + 10):
        info = calendar.session_on(day)
        if info is not None and info.close_at <= as_of:
            found.append(day)
            if len(found) == count:
                break
        day -= timedelta(days=1)
    return sorted(found)


def collect(
    provider: MarketDataProvider,
    symbols: Sequence[str],
    calendar: MarketCalendar,
    days: Sequence[date],
    root: Path,
    *,
    quotes: bool = True,
    log: Callable[[str], None] = lambda _msg: None,
) -> dict[str, int]:
    counts = {"bars_days": 0, "quote_days": 0, "skipped": 0}
    sessions = []
    for d in sorted(days):
        info = calendar.session_on(d)
        if info is None:
            raise ValueError(f"{d} is not a trading session")
        sessions.append(info)
    for info in sessions:
        path = root / info.session_date.isoformat() / "bars.csv"
        if path.exists():
            counts["skipped"] += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        n = write_bars(path, provider.fetch_bars(symbols, info.open_at, info.close_at))
        counts["bars_days"] += 1
        log(f"{info.session_date} bars: {n}")
    if quotes:
        for info in reversed(sessions):
            path = root / info.session_date.isoformat() / "quotes.csv"
            if path.exists():
                counts["skipped"] += 1
                continue
            n = write_quotes(path, provider.fetch_quotes(symbols, info.open_at, info.close_at))
            counts["quote_days"] += 1
            log(f"{info.session_date} quotes: {n}")
    return counts


def merge_days(root: Path, days: Sequence[date], target: Path) -> dict[str, int]:
    """Concatenate per-day files into the single bars.csv/quotes.csv the replay provider reads."""
    target.mkdir(parents=True, exist_ok=True)
    totals = {"days": 0, "bars": 0, "quotes": 0}
    for name, key in (("bars.csv", "bars"), ("quotes.csv", "quotes")):
        with (target / name).open("w") as out:
            wrote_header = False
            for d in sorted(days):
                source = root / d.isoformat() / name
                if not source.exists():
                    continue
                with source.open() as handle:
                    header = handle.readline()
                    if not wrote_header:
                        out.write(header)
                        wrote_header = True
                    shutil.copyfileobj(handle, out)
                    totals[key] += sum(1 for _ in source.open()) - 1
    totals["days"] = sum(1 for d in days if (root / d.isoformat() / "bars.csv").exists())
    return totals


def merged_dates(directory: Path) -> list[date]:
    """The distinct session dates present in a merged bars.csv."""
    found: set[date] = set()
    with (directory / "bars.csv").open() as handle:
        handle.readline()
        for line in handle:
            parts = line.split(",", 2)
            if len(parts) > 1 and parts[1][:10].count("-") == 2:
                found.add(date.fromisoformat(parts[1][:10]))
    return sorted(found)
