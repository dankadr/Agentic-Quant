"""Opening Range Breakout (long only). A research hypothesis, not validated edge.

Entry: after the opening range completes, a closed bar above range-high (plus a buffer) on
volume well above the range's average, with a sane spread, once per symbol per session.
Exit: stop at range-low, target at ``target_r`` multiples of the initial risk, or a time stop.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading.core.enums import Side
from trading.core.money import NoFloatDecimal, ceil_to_increment, floor_to_increment
from trading.data.models import Bar
from trading.quant.models import Holding, SignalProposal, StrategyInput

_CENT = Decimal("0.01")
_BPS = Decimal(10000)
# Marketable-limit padding versus the quote: a ceiling/floor, never the expected fill price.
_LIMIT_PAD_BPS = Decimal(10)
_ENTRY_WINDOW_END_BEFORE_CLOSE = timedelta(minutes=20)


class OrbParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    symbols: tuple[str, ...]
    opening_range_minutes: int = Field(gt=0, le=60)
    volume_confirm_ratio: NoFloatDecimal = Field(ge=0)
    breakout_buffer_bps: NoFloatDecimal = Field(ge=0)
    target_r: NoFloatDecimal = Field(gt=0)
    max_hold_minutes: int = Field(gt=0)
    max_spread_bps: NoFloatDecimal = Field(gt=0)
    min_avg_volume: NoFloatDecimal = Field(ge=0)
    trade_notional: NoFloatDecimal = Field(gt=0)
    signal_ttl_seconds: int = Field(gt=0, le=300)


class OrbStrategy:
    def __init__(self, *, strategy_id: str, version: str, params: Mapping[str, Any]) -> None:
        self.strategy_id = strategy_id
        self.version = version
        self.p = OrbParams.model_validate(dict(params))

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

    def evaluate(self, data: StrategyInput) -> Sequence[SignalProposal]:
        p = self.p
        open_at = data.session.open_at
        range_end = open_at + timedelta(minutes=p.opening_range_minutes)
        if data.now < range_end:
            return []
        signals: list[SignalProposal] = []
        for symbol in p.symbols:
            view = data.snapshot.symbols.get(symbol)
            bars = tuple(b for b in data.bars.get(symbol, ()) if b.available_at <= data.now)
            if view is None or not view.tradable or view.quote is None or not bars:
                continue
            range_bars = [b for b in bars if open_at <= b.ts < range_end]
            if len(range_bars) != p.opening_range_minutes:
                continue  # a gap in the opening range: no trade rather than a guess
            range_high = max(b.high for b in range_bars)
            range_low = min(b.low for b in range_bars)
            held = next(
                (
                    h
                    for h in data.holdings
                    if h.strategy_id == self.strategy_id and h.symbol == symbol and h.qty > 0
                ),
                None,
            )
            if held is not None:
                exit_signal = self._exit(data, symbol, held, bars[-1], view.quote.bid, range_low)
                if exit_signal is not None:
                    signals.append(exit_signal)
                continue
            entry = self._entry(data, symbol, bars, range_bars, range_high, range_low, view.quote)
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
        range_low: Decimal,
    ) -> SignalProposal | None:
        risk = held.avg_cost - range_low
        target = held.avg_cost + self.p.target_r * risk if risk > 0 else None
        if last.close <= range_low:
            reason = "STOP"
        elif target is not None and last.close >= target:
            reason = "TARGET"
        elif data.now - held.opened_at >= timedelta(minutes=self.p.max_hold_minutes):
            reason = "TIME_STOP"
        else:
            return None
        limit = floor_to_increment(bid * (1 - _LIMIT_PAD_BPS / _BPS), _CENT)
        if limit <= 0:
            return None
        return self._signal(data, symbol, Side.SELL, held.qty * last.close, limit, None, (reason,))

    def _entry(
        self, data: StrategyInput, symbol: str, bars: tuple[Bar, ...], range_bars: list[Bar],
        range_high: Decimal, range_low: Decimal, quote: Any,
    ) -> SignalProposal | None:  # fmt: skip
        p = self.p
        if (self.strategy_id, symbol) in data.entered_today:
            return None
        if data.now >= data.session.close_at - _ENTRY_WINDOW_END_BEFORE_CLOSE:
            return None
        last = bars[-1]
        average_volume = sum((b.volume for b in range_bars), Decimal(0)) / len(range_bars)
        if average_volume < p.min_avg_volume or quote.spread_bps > p.max_spread_bps:
            return None
        if last.close <= range_high * (1 + p.breakout_buffer_bps / _BPS):
            return None
        if last.volume < p.volume_confirm_ratio * average_volume:
            return None
        limit = ceil_to_increment(quote.ask * (1 + _LIMIT_PAD_BPS / _BPS), _CENT)
        if range_low >= limit:
            return None
        return self._signal(
            data,
            symbol,
            Side.BUY,
            p.trade_notional,
            limit,
            range_low,
            ("ORB_BREAK", "VOLUME_CONFIRM"),
        )
