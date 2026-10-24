"""Capital allocation: turn validated signals into sized, netted, attributed order proposals.

Sleeve semantics (decision C1): a strategy's ``allocation_pct`` is the MAXIMUM it may hold
(``pct x equity``); the symbol cap is shared by every strategy. Sizing floors to the quantity
increment, so rounding can never push notional above a cap. Opposing signals for one symbol net
into ONE order whose ``attribution`` (signed intended shares per strategy) always sums to the order,
so fills can be split back to strategies. Nothing here can create a short.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from trading.core.config import PaperBrokerConfig
from trading.core.enums import OrderType, Side
from trading.core.money import floor_to_increment
from trading.data.instruments import InstrumentInfo, qty_increment
from trading.execution.intents import ProposalRequest
from trading.quant.models import Holding, SignalProposal

FLATTEN_STRATEGY = "eod_flatten"
_ZERO = Decimal(0)


@dataclass(frozen=True)
class AllocationContext:
    equity: Decimal
    now: datetime
    marks: Mapping[str, Decimal]
    holdings: tuple[Holding, ...]  # per strategy
    held_qty: Mapping[str, Decimal]  # per symbol, from the portfolio (the broker-true total)
    open_buy_notional: Mapping[str, Decimal]
    open_sell_qty: Mapping[str, Decimal]
    in_flight: frozenset[tuple[str, str, str]]  # (strategy, symbol, side) with an open order
    instruments: Mapping[str, InstrumentInfo]
    sleeves: Mapping[str, Decimal]  # strategy -> allocation_pct
    max_symbol_pct: Decimal
    paper_cfg: PaperBrokerConfig


@dataclass(frozen=True)
class AllocationNote:
    signal_id: UUID
    strategy_id: str
    symbol: str
    side: str
    outcome: str  # ALLOCATED | DROPPED
    reason: str
    qty: Decimal


@dataclass(frozen=True)
class AllocationResult:
    requests: tuple[ProposalRequest, ...]
    notes: tuple[AllocationNote, ...]
    targets: dict[str, dict[str, object]]


class CapitalAllocator(Protocol):
    def allocate(
        self, signals: Sequence[SignalProposal], ctx: AllocationContext
    ) -> AllocationResult: ...


@dataclass
class _Leg:
    signal: SignalProposal
    strategy: str  # who the shares are attributed to
    qty: Decimal  # always positive


class SleeveAllocator:
    def allocate(
        self, signals: Sequence[SignalProposal], ctx: AllocationContext
    ) -> AllocationResult:
        by_symbol: dict[str, list[SignalProposal]] = defaultdict(list)
        for signal in sorted(signals, key=lambda s: (s.symbol, s.strategy_id, str(s.signal_id))):
            by_symbol[signal.symbol].append(signal)
        notes: dict[UUID, AllocationNote] = {}
        requests: list[ProposalRequest] = []
        targets: dict[str, dict[str, object]] = {}
        sleeve_used: dict[str, Decimal] = defaultdict(Decimal)
        holder_left: dict[tuple[str, str], Decimal] = {
            (h.strategy_id, h.symbol): h.qty for h in ctx.holdings if h.qty > 0
        }

        def drop(sig: SignalProposal, reason: str) -> None:
            notes[sig.signal_id] = AllocationNote(
                sig.signal_id, sig.strategy_id, sig.symbol, sig.side.value, "DROPPED", reason, _ZERO
            )

        for symbol, group in by_symbol.items():
            instrument = ctx.instruments.get(symbol)
            live: list[SignalProposal] = []
            for sig in group:
                if instrument is None:
                    drop(sig, "UNKNOWN_SYMBOL")
                elif ctx.now >= sig.expires_at:
                    drop(sig, "EXPIRED")
                elif (sig.strategy_id, symbol, sig.side.value) in ctx.in_flight:
                    drop(sig, "ORDER_IN_FLIGHT")
                else:
                    live.append(sig)
            if instrument is None or not live:
                continue
            inc = qty_increment(instrument, ctx.paper_cfg)
            mark = ctx.marks.get(symbol)
            sellable = max(
                ctx.held_qty.get(symbol, _ZERO) - ctx.open_sell_qty.get(symbol, _ZERO), _ZERO
            )

            sells: list[_Leg] = []
            for sig in (s for s in live if s.side is Side.SELL):
                if sig.strategy_id == FLATTEN_STRATEGY:
                    legs = self._flatten_legs(sig, symbol, holder_left, sellable, inc, ctx)
                else:
                    own = holder_left.get((sig.strategy_id, symbol), _ZERO)
                    qty = floor_to_increment(min(own, sellable), inc)
                    legs = [_Leg(sig, sig.strategy_id, qty)] if qty > 0 else []
                for leg in legs:
                    sellable -= leg.qty
                    key = (leg.strategy, symbol)
                    holder_left[key] = holder_left.get(key, _ZERO) - leg.qty
                sells.extend(legs)
                if not legs:
                    drop(sig, "NO_HOLDING")

            freed = sum((leg.qty for leg in sells), _ZERO) * (mark or _ZERO)
            headroom = max(
                ctx.max_symbol_pct * ctx.equity
                - ctx.held_qty.get(symbol, _ZERO) * (mark or _ZERO)
                - ctx.open_buy_notional.get(symbol, _ZERO)
                + freed,
                _ZERO,
            )
            freed_by_strategy: dict[str, Decimal] = defaultdict(Decimal)
            for leg in sells:
                freed_by_strategy[leg.strategy] += leg.qty * (mark or _ZERO)

            buys: list[_Leg] = []
            for sig in (s for s in live if s.side is Side.BUY):
                price = sig.limit_price or mark
                pct = ctx.sleeves.get(sig.strategy_id, _ZERO)
                if price is None or price <= 0 or pct <= 0:
                    drop(sig, "NO_BUDGET")
                    continue
                exposure = sum(
                    (
                        h.qty * ctx.marks.get(h.symbol, h.avg_cost)
                        for h in ctx.holdings
                        if h.strategy_id == sig.strategy_id
                    ),
                    _ZERO,
                )
                sleeve_left = (
                    pct * ctx.equity - exposure - sleeve_used[sig.strategy_id]
                    + freed_by_strategy[sig.strategy_id]
                )  # fmt: skip
                cap = min(sig.target_notional_usd, sleeve_left, headroom)
                if cap <= 0:
                    drop(sig, "NO_BUDGET")
                    continue
                qty = floor_to_increment(cap / price, inc)
                if qty <= 0:
                    drop(sig, "QTY_BELOW_INCREMENT")
                    continue
                headroom -= qty * price
                sleeve_used[sig.strategy_id] += qty * price
                buys.append(_Leg(sig, sig.strategy_id, qty))

            legs_all = [*buys, *sells]
            if not legs_all:
                continue
            net = sum((leg.qty for leg in buys), _ZERO) - sum((leg.qty for leg in sells), _ZERO)
            if net == 0:
                for leg in legs_all:
                    drop(leg.signal, "NETTED_TO_ZERO")
                continue
            side = Side.BUY if net > 0 else Side.SELL
            attribution: dict[str, Decimal] = defaultdict(Decimal)
            for leg in buys:
                attribution[leg.strategy] += leg.qty
            for leg in sells:
                attribution[leg.strategy] -= leg.qty
            order = sorted(attribution, key=lambda k: (-abs(attribution[k]), k))
            if side is Side.BUY:
                limits = [leg.signal.limit_price or mark for leg in buys]
                limit = max(x for x in limits if x is not None)
            else:
                limits = [leg.signal.limit_price or mark for leg in sells]
                limit = min(x for x in limits if x is not None)
            contributing = {leg.signal.signal_id: leg.signal for leg in legs_all}
            request = ProposalRequest(
                symbol=symbol,
                side=side,
                quantity=abs(net),
                order_type=OrderType.LIMIT,
                limit_price=limit,
                strategy_id=order[0],
                signal_id=uuid5(NAMESPACE_URL, "|".join(sorted(str(i) for i in contributing))),
                signal_expires_at=min(s.expires_at for s in contributing.values()),
                strategy_ids=tuple(order),
                attribution={k: attribution[k] for k in order},
            )
            requests.append(request)
            targets[symbol] = {
                "side": side.value,
                "net_qty": str(request.quantity),
                "strategies": list(order),
                "attribution": {k: str(v) for k, v in request.attribution.items()},
            }
            for sig in contributing.values():
                notes[sig.signal_id] = AllocationNote(
                    sig.signal_id, sig.strategy_id, symbol, sig.side.value, "ALLOCATED", "NETTED",
                    abs(net),
                )  # fmt: skip
        ordered_notes = tuple(
            sorted(notes.values(), key=lambda n: (n.symbol, n.strategy_id, str(n.signal_id)))
        )
        return AllocationResult(tuple(requests), ordered_notes, targets)

    @staticmethod
    def _flatten_legs(
        sig: SignalProposal,
        symbol: str,
        holder_left: dict[tuple[str, str], Decimal],
        sellable: Decimal,
        inc: Decimal,
        ctx: AllocationContext,
    ) -> list[_Leg]:
        """Sell every holder's shares; shares nobody owns are attributed to the flatten strategy."""
        legs: list[_Leg] = []
        remaining = sellable
        owned = _ZERO
        for (strategy, sym), qty in sorted(holder_left.items()):
            if sym != symbol or qty <= 0:
                continue
            owned += qty
            take = floor_to_increment(min(qty, remaining), inc)
            if take > 0:
                legs.append(_Leg(sig, strategy, take))
                remaining -= take
        unowned = floor_to_increment(
            min(max(ctx.held_qty.get(symbol, _ZERO) - owned, _ZERO), remaining), inc
        )
        if unowned > 0:
            legs.append(_Leg(sig, FLATTEN_STRATEGY, unowned))
        return legs
