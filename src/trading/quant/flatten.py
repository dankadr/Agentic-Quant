"""End-of-day flatten: sell everything that is still held shortly before the close.

Exits are never blocked by the entry cutoff or by loss/drawdown halts (the risk engine allows
them); this strategy only decides WHEN to ask. Unsold leftovers are recorded by ``session.end``.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from trading.core.enums import Side
from trading.core.money import floor_to_increment
from trading.quant.models import SignalProposal, StrategyInput

_CENT = Decimal("0.01")
_BPS = Decimal(10000)
_PAD_BPS = Decimal(20)  # wide marketable limit: closing the book matters more than the last tick
_TTL_SECONDS = 60


class FlattenParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    flatten_minutes_before_close: int = Field(gt=0, le=120)


class FlattenStrategy:
    def __init__(self, *, strategy_id: str, version: str, params: Mapping[str, Any]) -> None:
        self.strategy_id = strategy_id
        self.version = version
        self.p = FlattenParams.model_validate(dict(params))

    def evaluate(self, data: StrategyInput) -> Sequence[SignalProposal]:
        close_at = data.session.close_at
        in_window = (
            close_at - timedelta(minutes=self.p.flatten_minutes_before_close) <= data.now < close_at
        )
        if not (data.flatten_now or in_window) or data.now >= close_at:
            return []
        signals: list[SignalProposal] = []
        for symbol, qty in sorted(data.positions.items()):
            view = data.snapshot.symbols.get(symbol)
            if qty <= 0 or view is None or view.quote is None or view.quote.bid <= 0:
                continue
            limit = floor_to_increment(view.quote.bid * (1 - _PAD_BPS / _BPS), _CENT)
            if limit <= 0:
                continue
            signals.append(
                SignalProposal(
                    strategy_id=self.strategy_id,
                    strategy_version=self.version,
                    symbol=symbol,
                    side=Side.SELL,
                    generated_at=data.now,
                    expires_at=data.now + timedelta(seconds=_TTL_SECONDS),
                    market_snapshot_id=data.snapshot.id,
                    target_notional_usd=qty * view.quote.mid,
                    stop_price=None,
                    limit_price=limit,
                    reason_codes=("EOD_FLATTEN",),
                )
            )
        return signals
