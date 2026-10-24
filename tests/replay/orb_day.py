"""A deterministic SYNTHETIC trading day on SPY that exercises ORB end to end."""

from decimal import Decimal
from pathlib import Path

D = Decimal
OPEN_H, OPEN_M = 13, 30


def _ts(minute: int, second: int = 0) -> str:
    total = OPEN_H * 60 + OPEN_M + minute
    return f"2026-10-19T{total // 60:02d}:{total % 60:02d}:{second:02d}Z"


def write_orb_day(
    path: Path,
    *,
    rally_to: str | None = "101.20",
    breakout_volume: int = 1500,
    later_volume: int = 1200,
    symbol: str = "SPY",
) -> Path:
    """15 range bars (99.90-100.10), a breakout bar at minute 15, an optional rally in 5-cent steps,
    then flat. rally_to=None keeps price just above the range (no target, no stop)."""
    path.mkdir(parents=True, exist_ok=True)
    closes: list[Decimal] = [D("100.00")] * 15 + [D("100.30")]
    price = D("100.30")
    while len(closes) < 390:
        if rally_to is not None and price < D(rally_to):
            price += D("0.05")
        closes.append(price)
    bars = ["# SYNTHETIC — not market data", "symbol,ts,open,high,low,close,volume,source_seq"]
    quotes = ["# SYNTHETIC — not market data", "symbol,ts,bid,ask,bid_size,ask_size"]
    for minute, close in enumerate(closes):
        if minute < 15:
            o, h, lo, vol = D("100.00"), D("100.10"), D("99.90"), 1000
        elif minute == 15:
            o, h, lo, vol = D("100.05"), D("100.35"), D("100.05"), breakout_volume
        else:
            o, h, lo, vol = close, close + D("0.02"), close - D("0.02"), later_volume
        bars.append(f"{symbol},{_ts(minute)},{o},{h},{lo},{close},{vol},{symbol}-{minute}")
        quotes.append(f"{symbol},{_ts(minute, 59)},{close - D('0.01')},{close + D('0.01')},500,500")
    (path / "bars.csv").write_text("\n".join(bars) + "\n")
    (path / "quotes.csv").write_text("\n".join(quotes) + "\n")
    return path
