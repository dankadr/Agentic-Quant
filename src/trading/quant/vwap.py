"""VWAP mean reversion (long only). A research hypothesis, not validated edge.

Entry: price stretched below session VWAP (z-score <= -z_entry but not beyond -z_stop, which
would be a falling knife), evidence of a turn (last close above the previous), and a flat-enough
VWAP (strongly trending tape is rejected). Exit: back at VWAP, z-stop, or a time stop.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading.core.enums import Side
from trading.core.money import NoFloatDecimal, ceil_to_increment, floor_to_increment
from trading.data.models import Bar
from trading.quant.indicators import rolling_std, slope_bps, vwap_series
from trading.quant.models import Holding, SignalProposal, StrategyInput

_CENT = Decimal("0.01")
_BPS = Decimal(10000)
_LIMIT_PAD_BPS = Decimal(10)
_ENTRY_WINDOW_END_BEFORE_CLOSE = timedelta(minutes=20)


class VwapParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbols: tuple[str, ...]
    lookback_bars: int = Field(gt=1, le=240)
    min_bars: int = Field(gt=1, le=390)
    z_entry: NoFloatDecimal = Field(gt=0)
    z_stop: NoFloatDecimal = Field(gt=0)
    max_slope_bps: NoFloatDecimal = Field(ge=0)
    max_hold_minutes: int = Field(gt=0)
    max_spread_bps: NoFloatDecimal = Field(gt=0)
    trade_notional: NoFloatDecimal = Field(gt=0)
    signal_ttl_seconds: int = Field(gt=0, le=300)


class VwapStrategy:
    def __init__(self, *, strategy_id: str, version: str, params: Mapping[str, Any]) -> None:
        self.strategy_id = strategy_id
        self.version = version
        self.p = VwapParams.model_validate(dict(params))
        if self.p.z_stop <= self.p.z_entry:
            raise ValueError("z_stop must be beyond z_entry")
        if self.p.min_bars < self.p.lookback_bars:
            raise ValueError("min_bars must cover lookback_bars")

    def _signal(
        self, data: StrategyInput, symbol: str, side: Side, notional: Decimal, limit: Decimal,
        stop: Decimal | None, reasons: tuple[str, ...],
    ) -> SignalProposal:  # fmt: skip
        return SignalProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            symbol=symbol,
            side=side,
            generated_at=data.now,
            expires_at=data.now + timedelta(seconds=self.p.signal_ttl_seconds),
            market_snapshot_id=data.snapshot.id,
            target_notional_usd=notional,
            stop_price=stop,
            limit_price=limit,
            reason_codes=reasons,
        )

    def _stats(self, bars: Sequence[Bar]) -> tuple[Decimal, Decimal, Decimal] | None:
        """(vwap, z-score, std of deviations) of the last bar, or None if undefined."""
        vwaps = vwap_series(bars)
        deviations = [b.close - v for b, v in zip(bars, vwaps, strict=True) if v is not None]
        std = rolling_std(deviations, self.p.lookback_bars)
        last_vwap = vwaps[-1]
        if std is None or std == 0 or last_vwap is None:
            return None
        return last_vwap, deviations[-1] / std, std

    def evaluate(self, data: StrategyInput) -> Sequence[SignalProposal]:
        p = self.p
        signals: list[SignalProposal] = []
        for symbol in p.symbols:
            view = data.snapshot.symbols.get(symbol)
            bars = tuple(b for b in data.bars.get(symbol, ()) if b.available_at <= data.now)
            if view is None or not view.tradable or view.quote is None or len(bars) < p.min_bars:
                continue
            stats = self._stats(bars)
            if stats is None:
                continue
            vwap, z, std = stats
            held = next(
                (
                    h
                    for h in data.holdings
                    if h.strategy_id == self.strategy_id and h.symbol == symbol and h.qty > 0
                ),
                None,
            )
            if held is not None:
                exit_signal = self._exit(data, symbol, held, bars[-1], view.quote.bid, vwap, z)
                if exit_signal is not None:
                    signals.append(exit_signal)
                continue
            entry = self._entry(data, symbol, bars, view.quote, vwap, z, std)
            if entry is not None:
                signals.append(entry)
        return signals

    def _exit(
        self,
        data: StrategyInput,
        symbol: str,
        held: Holding,
        last: Bar,
        bid: Decimal,
        vwap: Decimal,
        z: Decimal,
    ) -> SignalProposal | None:
        if last.close >= vwap:
            reason = "VWAP_REVERSION"
        elif z <= -self.p.z_stop:
            reason = "Z_STOP"
        elif data.now - held.opened_at >= timedelta(minutes=self.p.max_hold_minutes):
            reason = "TIME_STOP"
        else:
            return None
        limit = floor_to_increment(bid * (1 - _LIMIT_PAD_BPS / _BPS), _CENT)
        if limit <= 0:
            return None
        return self._signal(data, symbol, Side.SELL, held.qty * last.close, limit, None, (reason,))

    def _entry(
        self,
        data: StrategyInput,
        symbol: str,
        bars: tuple[Bar, ...],
        quote: Any,
        vwap: Decimal,
        z: Decimal,
        std: Decimal,
    ) -> SignalProposal | None:
        p = self.p
        if (self.strategy_id, symbol) in data.entered_today:
            return None
        if data.now >= data.session.close_at - _ENTRY_WINDOW_END_BEFORE_CLOSE:
            return None
        if quote.spread_bps > p.max_spread_bps:
            return None
        if not (-p.z_stop < z <= -p.z_entry):  # stretched, but not a falling knife
            return None
        if bars[-1].close <= bars[-2].close:  # no evidence of a turn yet
            return None
        slope = slope_bps(vwap_series(bars), p.lookback_bars)
        if slope is None or abs(slope) > p.max_slope_bps:  # strongly trending tape
            return None
        limit = ceil_to_increment(quote.ask * (1 + _LIMIT_PAD_BPS / _BPS), _CENT)
        stop = floor_to_increment(vwap - p.z_stop * std, _CENT)
        if stop <= 0 or stop >= limit:
            return None
        return self._signal(
            data,
            symbol,
            Side.BUY,
            p.trade_notional,
            limit,
            stop,
            ("VWAP_STRETCH", "REVERSION_TURN"),
        )
