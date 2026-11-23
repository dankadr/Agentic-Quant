"""Opening Range Breakout. A research hypothesis, not validated edge.

Entry: after the opening range completes, a closed bar above range-high (plus a buffer) on
volume well above the range's average, with a sane spread and a bounded daily entry budget.
Exit: stop at range-low, target at ``target_r`` multiples of the initial risk, or a time stop.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading.core.enums import PositionEffect, Side, effective_effect
from trading.core.money import NoFloatDecimal, ceil_to_increment, floor_to_increment
from trading.data.models import Bar
from trading.quant.models import Holding, SignalProposal, StrategyInput, entry_block_reason

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
    max_entries_per_symbol_per_day: int = Field(default=1, gt=0, le=10)
    reentry_cooldown_minutes: int = Field(default=30, ge=0, le=390)


class OrbStrategy:
    def __init__(self, *, strategy_id: str, version: str, params: Mapping[str, Any]) -> None:
        self.strategy_id = strategy_id
        self.version = version
        self.p = OrbParams.model_validate(dict(params))

    def _signal(
        self, data: StrategyInput, symbol: str, side: Side, notional: Decimal, limit: Decimal,
        stop: Decimal | None, reasons: tuple[str, ...], effect: PositionEffect | None = None,
    ) -> SignalProposal:  # fmt: skip
        return SignalProposal(
            strategy_id=self.strategy_id,
            strategy_version=self.version,
            symbol=symbol,
            side=side,
            position_effect=effective_effect(side, effect),
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
        self.entry_diagnostics = dict.fromkeys(p.symbols, "NO_SIGNAL")
        open_at = data.session.open_at
        range_end = open_at + timedelta(minutes=p.opening_range_minutes)
        if data.now < range_end:
            return []
        signals: list[SignalProposal] = []
        for symbol in p.symbols:
            view = data.snapshot.symbols.get(symbol)
            bars = tuple(b for b in data.bars.get(symbol, ()) if b.available_at <= data.now)
            if view is None or not view.tradable or view.quote is None or not bars:
                self.entry_diagnostics[symbol] = "DATA_UNAVAILABLE"
                continue
            range_bars = [b for b in bars if open_at <= b.ts < range_end]
            if len(range_bars) != p.opening_range_minutes:
                self.entry_diagnostics[symbol] = "DATA_UNAVAILABLE"
                continue  # a gap in the opening range: no trade rather than a guess
            range_high = max(b.high for b in range_bars)
            range_low = min(b.low for b in range_bars)
            held = next(
                (
                    h
                    for h in data.holdings
                    if h.strategy_id == self.strategy_id and h.symbol == symbol and h.qty != 0
                ),
                None,
            )
            if held is not None:
                self.entry_diagnostics[symbol] = "HOLDING"
                exit_signal = self._exit(
                    data,
                    symbol,
                    held,
                    bars[-1],
                    view.quote,
                    range_high if held.qty < 0 else range_low,
                )
                if exit_signal is not None:
                    signals.append(exit_signal)
                continue
            entry = self._entry(data, symbol, bars, range_bars, range_high, range_low, view.quote)
            if entry is not None:
                self.entry_diagnostics[symbol] = "SIGNAL"
                signals.append(entry)
        return signals

    def _exit(
        self,
        data: StrategyInput,
        symbol: str,
        held: Holding,
        last: Bar,
        quote: Any,
        stop: Decimal,
    ) -> SignalProposal | None:
        direction = 1 if held.qty > 0 else -1
        risk = direction * (held.avg_cost - stop)
        target = held.avg_cost + direction * self.p.target_r * risk if risk > 0 else None
        if direction * (last.close - stop) <= 0:
            reason = "STOP"
        elif target is not None and direction * (last.close - target) >= 0:
            reason = "TARGET"
        elif data.now - held.opened_at >= timedelta(minutes=self.p.max_hold_minutes):
            reason = "TIME_STOP"
        else:
            return None
        if held.qty < 0:
            limit = ceil_to_increment(quote.ask * (1 + _LIMIT_PAD_BPS / _BPS), _CENT)
            return self._signal(
                data,
                symbol,
                Side.BUY,
                -held.qty * last.close,
                limit,
                None,
                (reason,),
                PositionEffect.CLOSE_SHORT,
            )
        limit = floor_to_increment(quote.bid * (1 - _LIMIT_PAD_BPS / _BPS), _CENT)
        if limit <= 0:
            return None
        return self._signal(data, symbol, Side.SELL, held.qty * last.close, limit, None, (reason,))

    def _entry(
        self, data: StrategyInput, symbol: str, bars: tuple[Bar, ...], range_bars: list[Bar],
        range_high: Decimal, range_low: Decimal, quote: Any,
    ) -> SignalProposal | None:  # fmt: skip
        p = self.p
        blocked = entry_block_reason(
            data,
            self.strategy_id,
            symbol,
            maximum=p.max_entries_per_symbol_per_day,
            cooldown_minutes=p.reentry_cooldown_minutes,
        )
        if blocked is not None:
            self.entry_diagnostics[symbol] = blocked
            return None
        if data.now >= data.session.close_at - _ENTRY_WINDOW_END_BEFORE_CLOSE:
            self.entry_diagnostics[symbol] = "SESSION_ENTRY_CUTOFF"
            return None
        last = bars[-1]
        average_volume = sum((b.volume for b in range_bars), Decimal(0)) / len(range_bars)
        if average_volume < p.min_avg_volume or quote.spread_bps > p.max_spread_bps:
            return None
        if last.volume < p.volume_confirm_ratio * average_volume:
            return None
        if data.allow_short and last.close < range_low * (1 - p.breakout_buffer_bps / _BPS):
            limit = floor_to_increment(quote.bid * (1 - _LIMIT_PAD_BPS / _BPS), _CENT)
            if limit <= 0 or limit >= range_high:
                return None
            return self._signal(
                data,
                symbol,
                Side.SELL,
                p.trade_notional,
                limit,
                range_high,
                ("ORB_BREAK", "VOLUME_CONFIRM"),
                PositionEffect.OPEN_SHORT,
            )
        if last.close <= range_high * (1 + p.breakout_buffer_bps / _BPS):
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
