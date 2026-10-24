"""Deterministic market features handed to the macro agent. Numbers only: no free text ever
reaches the model, so there is no prompt-injection surface in this agent's input."""

import itertools
from collections.abc import Mapping, Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from trading.data.models import Bar
from trading.quant.indicators import session_vwap

_ZERO = Decimal(0)
_BPS = Decimal(10000)


class IndexFeatures(BaseModel):
    model_config = ConfigDict(frozen=True)
    return_pct: Decimal  # open -> last close, as a fraction
    realized_vol_bps: Decimal  # std of 1-minute returns, in basis points
    efficiency: Decimal  # |net move| / sum of |minute moves|: 1 = straight line, ~0 = chop
    range_pct: Decimal  # (session high - low) / open


class MacroFeatures(BaseModel):
    model_config = ConfigDict(frozen=True)
    bars: int
    breadth_above_vwap: Decimal  # share of symbols whose last close is above their session VWAP
    indices: dict[str, IndexFeatures]


def _index(bars: Sequence[Bar]) -> IndexFeatures:
    first_open, last_close = bars[0].open, bars[-1].close
    returns = [(b.close - p.close) / p.close for p, b in itertools.pairwise(bars) if p.close > 0]
    mean = sum(returns, _ZERO) / len(returns) if returns else _ZERO
    variance = sum(((r - mean) ** 2 for r in returns), _ZERO) / len(returns) if returns else _ZERO
    path = sum((abs(b.close - p.close) for p, b in itertools.pairwise(bars)), _ZERO)
    net = abs(last_close - bars[0].close)
    high = max(b.high for b in bars)
    low = min(b.low for b in bars)
    return IndexFeatures(
        return_pct=(last_close - first_open) / first_open if first_open > 0 else _ZERO,
        realized_vol_bps=variance.sqrt() * _BPS,
        efficiency=net / path if path > 0 else _ZERO,
        range_pct=(high - low) / first_open if first_open > 0 else _ZERO,
    )


def compute_macro_features(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    *,
    index_symbols: Sequence[str],
    min_bars: int,
) -> MacroFeatures | None:
    """None when any index has fewer than ``min_bars`` bars (the agent then does not run)."""
    if any(len(bars_by_symbol.get(s, ())) < min_bars for s in index_symbols):
        return None
    above = total = 0
    for bars in bars_by_symbol.values():
        vwap = session_vwap(bars)
        if vwap is None or not bars:
            continue
        total += 1
        above += 1 if bars[-1].close > vwap else 0
    return MacroFeatures(
        bars=min(len(bars_by_symbol[s]) for s in index_symbols),
        breadth_above_vwap=Decimal(above) / Decimal(total) if total else _ZERO,
        indices={s: _index(bars_by_symbol[s]) for s in index_symbols},
    )
