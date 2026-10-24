"""Deterministic indicators over timestamped bars. Decimal only; no randomness; no look-ahead
(each value depends solely on the bars passed in)."""

from collections.abc import Sequence
from decimal import Decimal

from trading.data.models import Bar

_BPS = Decimal(10000)
_THREE = Decimal(3)


def typical_price(bar: Bar) -> Decimal:
    return (bar.high + bar.low + bar.close) / _THREE


def vwap_series(bars: Sequence[Bar]) -> list[Decimal | None]:
    """Cumulative session VWAP after each bar (None until some volume has traded)."""
    out: list[Decimal | None] = []
    volume = Decimal(0)
    notional = Decimal(0)
    for bar in bars:
        volume += bar.volume
        notional += typical_price(bar) * bar.volume
        out.append(notional / volume if volume > 0 else None)
    return out


def session_vwap(bars: Sequence[Bar]) -> Decimal | None:
    series = vwap_series(bars)
    return series[-1] if series else None


def rolling_std(values: Sequence[Decimal], n: int) -> Decimal | None:
    """Population standard deviation of the last ``n`` values; None if fewer than ``n``."""
    if n <= 0 or len(values) < n:
        return None
    window = values[-n:]
    mean = sum(window, Decimal(0)) / n
    variance = sum(((v - mean) ** 2 for v in window), Decimal(0)) / n
    return variance.sqrt()


def zscore_vs_vwap(bars: Sequence[Bar], lookback: int) -> Decimal | None:
    """How many standard deviations the last close sits from VWAP, vs the last ``lookback`` bars."""
    vwaps = vwap_series(bars)
    deviations = [b.close - v for b, v in zip(bars, vwaps, strict=True) if v is not None]
    if len(deviations) < lookback:
        return None
    std = rolling_std(deviations, lookback)
    if std is None or std == 0:
        return None
    return deviations[-1] / std


def atr(bars: Sequence[Bar], n: int) -> Decimal | None:
    """Average true range over the last ``n`` bars (needs n + 1 bars)."""
    if n <= 0 or len(bars) < n + 1:
        return None
    ranges = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(bars[-n - 1 : -1], bars[-n:], strict=True)
    ]
    return sum(ranges, Decimal(0)) / n


def slope_bps(series: Sequence[Decimal | None], n: int) -> Decimal | None:
    """Change over the last ``n`` steps as basis points of the starting value."""
    if len(series) <= n:
        return None
    start, end = series[-n - 1], series[-1]
    if start is None or end is None or start == 0:
        return None
    return (end - start) / start * _BPS
