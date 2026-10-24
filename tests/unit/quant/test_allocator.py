from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading.core.config import load_config
from trading.core.enums import AssetType, OrderType, Side
from trading.data.instruments import InstrumentInfo
from trading.quant.allocator import AllocationContext, SleeveAllocator
from trading.quant.models import Holding, SignalProposal

CFG = load_config(Path("configs"))
D = Decimal
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)


def instruments(fractional: bool = True) -> dict[str, InstrumentInfo]:
    return {
        s: InstrumentInfo(id=uuid4(), symbol=s, asset_type=AssetType.ETF, tick_size=D("0.01"), lot_size=D(1),
                          fractional_eligible=fractional, sector="x")
        for s in ("SPY", "QQQ")
    }  # fmt: skip


def signal(
    strategy="orb_v1", symbol="SPY", side=Side.BUY, notional="80", limit="100.10", ttl=60, **kw
) -> SignalProposal:  # type: ignore[no-untyped-def]
    stop = D("99.00") if side is Side.BUY else None
    return SignalProposal(strategy_id=strategy, strategy_version="1.0.0", symbol=symbol, side=side, generated_at=NOW,
                          expires_at=NOW + timedelta(seconds=ttl), market_snapshot_id=kw.get("snap", uuid4()),
                          target_notional_usd=D(notional), stop_price=stop, limit_price=D(limit) if limit else None,
                          reason_codes=("T",))  # fmt: skip


def ctx(**over) -> AllocationContext:  # type: ignore[no-untyped-def]
    base = dict(
        equity=D("1000"), now=NOW, marks={"SPY": D("100"), "QQQ": D("100")}, holdings=(), held_qty={}, open_buy_notional={},
        open_sell_qty={}, in_flight=frozenset(), instruments=instruments(),
        sleeves={s: c.allocation_pct for s, c in CFG.strategies.items()}, max_symbol_pct=D("0.10"), paper_cfg=CFG.paper_broker,
    )  # fmt: skip
    base.update(over)
    return AllocationContext(**base)


def hold(strategy: str, qty: str, symbol: str = "SPY", cost: str = "100") -> Holding:
    return Holding(strategy, symbol, D(qty), D(cost), NOW - timedelta(minutes=10))


alloc = SleeveAllocator()


def test_buy_is_sized_from_target_notional_and_floored_to_the_increment() -> None:
    result = alloc.allocate([signal()], ctx())
    (req,) = result.requests
    assert (req.symbol, req.side, req.order_type, req.strategy_id) == (
        "SPY",
        Side.BUY,
        OrderType.LIMIT,
        "orb_v1",
    )
    assert req.quantity == D("0.7992")  # floor(80 / 100.10, 0.0001)
    assert req.quantity * D("100.10") <= D(
        "80"
    )  # rounding can never push notional above the target
    assert req.limit_price == D("100.10")
    assert req.attribution == {"orb_v1": D("0.7992")}
    assert req.signal_expires_at == NOW + timedelta(seconds=60)


def test_whole_share_instruments_floor_to_whole_shares() -> None:
    result = alloc.allocate(
        [signal(notional="250", limit="100.00")],
        ctx(instruments=instruments(fractional=False), equity=D("5000")),
    )
    assert result.requests[0].quantity == D("2")


def test_sleeve_remaining_limits_size() -> None:
    held = (hold("orb_v1", "1.9", symbol="QQQ"),)
    result = alloc.allocate(
        [signal(notional="80")],
        ctx(holdings=held, held_qty={"QQQ": D("1.9")}, max_symbol_pct=D("1")),
    )
    (req,) = result.requests
    assert req.quantity == D("0.0999")  # remaining sleeve = 200 - 190 = 10 -> floor(10 / 100.10)


def test_symbol_cap_limits_size_and_counts_existing_exposure() -> None:
    result = alloc.allocate(
        [signal(notional="80")], ctx(held_qty={"SPY": D("0.6")}, open_buy_notional={"SPY": D("10")})
    )
    (req,) = result.requests
    assert req.quantity == D(
        "0.2997"
    )  # headroom = 10% of 1000 - 60 - 10 = 30 -> floor(30 / 100.10)


def test_no_headroom_means_no_order_with_a_reason() -> None:
    result = alloc.allocate([signal()], ctx(held_qty={"SPY": D("1.0")}))
    assert result.requests == ()
    assert [(n.outcome, n.reason) for n in result.notes] == [("DROPPED", "NO_BUDGET")]


def test_strategy_without_a_sleeve_cannot_enter() -> None:
    sleeves = {**ctx().sleeves, "orb_v1": D("0")}
    result = alloc.allocate([signal()], ctx(sleeves=sleeves))
    assert [(n.outcome, n.reason) for n in result.notes] == [("DROPPED", "NO_BUDGET")]


def test_quantity_below_the_increment_is_dropped() -> None:
    result = alloc.allocate([signal(notional="0.005")], ctx())
    assert [(n.outcome, n.reason) for n in result.notes] == [("DROPPED", "QTY_BELOW_INCREMENT")]


def test_expired_signals_and_unknown_symbols_are_dropped() -> None:
    late = ctx(now=NOW + timedelta(seconds=61))
    assert alloc.allocate([signal()], late).notes[0].reason == "EXPIRED"
    unknown = signal(symbol="ZZZ")
    assert alloc.allocate([unknown], ctx()).notes[0].reason == "UNKNOWN_SYMBOL"


def test_signals_with_an_order_already_in_flight_are_dropped() -> None:
    result = alloc.allocate([signal()], ctx(in_flight=frozenset({("orb_v1", "SPY", "BUY")})))
    assert [(n.outcome, n.reason) for n in result.notes] == [("DROPPED", "ORDER_IN_FLIGHT")]


# ---- exits ---------------------------------------------------------------------------------------


def test_exit_sells_the_strategys_holding_never_more() -> None:
    held = (hold("orb_v1", "0.8"),)
    (req,) = alloc.allocate(
        [signal(side=Side.SELL, notional="80", limit="99.50")],
        ctx(holdings=held, held_qty={"SPY": D("0.8")}),
    ).requests
    assert (req.side, req.quantity, req.limit_price) == (Side.SELL, D("0.8"), D("99.50"))
    assert req.attribution == {"orb_v1": D("-0.8")}


def test_never_sells_more_than_is_held_or_unreserved() -> None:
    held = (hold("orb_v1", "0.8"),)
    result = alloc.allocate(
        [signal(side=Side.SELL)],
        ctx(holdings=held, held_qty={"SPY": D("0.8")}, open_sell_qty={"SPY": D("0.5")}),
    )
    (req,) = result.requests
    assert req.quantity == D("0.3")  # 0.8 held - 0.5 already being sold


def test_exit_without_a_holding_is_dropped() -> None:
    result = alloc.allocate([signal(side=Side.SELL)], ctx())
    assert [(n.outcome, n.reason) for n in result.notes] == [("DROPPED", "NO_HOLDING")]


# ---- netting + attribution (acceptance test 8) -----------------------------------------------------


def test_opposing_strategies_net_to_one_order_with_correct_attribution() -> None:
    held = (hold("vwap_v1", "0.3"),)
    signals = [signal("orb_v1", side=Side.BUY, notional="80", limit="100.10"),
               signal("vwap_v1", side=Side.SELL, notional="30", limit="99.80")]  # fmt: skip
    result = alloc.allocate(signals, ctx(holdings=held, held_qty={"SPY": D("0.3")}))
    (req,) = result.requests
    assert (req.symbol, req.side) == ("SPY", Side.BUY)
    assert req.quantity == D("0.4992")  # 0.7992 buy - 0.3 sell
    assert req.attribution == {"orb_v1": D("0.7992"), "vwap_v1": D("-0.3")}
    assert sum(req.attribution.values()) == req.quantity  # attribution always sums to the order
    assert req.strategy_ids == ("orb_v1", "vwap_v1")
    assert req.strategy_id == "orb_v1"  # the largest contributor is primary
    assert req.limit_price == D("100.10")  # the most permissive BUY limit


def test_netting_never_increases_exposure_beyond_the_cap() -> None:
    """At the symbol cap, an exit and an entry offset: the net order can only shrink exposure."""
    held = (hold("vwap_v1", "0.3"),)  # 1.0 held in total = the whole 10% cap; 0.7 belongs to nobody
    signals = [
        signal("orb_v1", side=Side.BUY, notional="80"),
        signal("vwap_v1", side=Side.SELL, notional="30", limit="99.80"),
    ]
    result = alloc.allocate(signals, ctx(holdings=held, held_qty={"SPY": D("1.0")}))
    assert all(r.side is Side.SELL for r in result.requests)
    assert sum(r.quantity for r in result.requests) <= D("0.3")


def test_a_net_sell_when_the_exit_is_larger() -> None:
    held = (hold("vwap_v1", "1.0"),)
    signals = [signal("orb_v1", side=Side.BUY, notional="20", limit="100.00"),
               signal("vwap_v1", side=Side.SELL, notional="100", limit="99.50")]  # fmt: skip
    result = alloc.allocate(signals, ctx(holdings=held, held_qty={"SPY": D("1.0")}))
    (req,) = result.requests
    assert (req.side, req.quantity) == (Side.SELL, D("0.8"))
    assert req.attribution == {"orb_v1": D("0.2"), "vwap_v1": D("-1.0")}
    assert req.limit_price == D("99.50")  # the most aggressive SELL limit


def test_exactly_offsetting_signals_produce_no_order() -> None:
    held = (hold("vwap_v1", "0.8"),)
    signals = [
        signal("orb_v1", side=Side.BUY, notional="80", limit="100.00"),
        signal("vwap_v1", side=Side.SELL, limit="99.00"),
    ]
    result = alloc.allocate(signals, ctx(holdings=held, held_qty={"SPY": D("0.8")}))
    assert result.requests == ()
    assert {n.reason for n in result.notes} == {"NETTED_TO_ZERO"}


def test_never_creates_a_short_through_netting() -> None:
    held = (hold("vwap_v1", "0.3"),)
    sells = [signal("vwap_v1", side=Side.SELL), signal("orb_v1", side=Side.SELL)]
    result = alloc.allocate(sells, ctx(holdings=held, held_qty={"SPY": D("0.3")}))
    (req,) = result.requests
    assert req.quantity <= D("0.3")
    assert req.attribution == {"vwap_v1": D("-0.3")}  # orb_v1 holds nothing: its sell is dropped
    assert any(n.reason == "NO_HOLDING" for n in result.notes)


def test_two_strategies_buying_the_same_symbol_share_the_symbol_cap() -> None:
    signals = [signal("orb_v1", notional="80"), signal("vwap_v1", notional="80")]
    result = alloc.allocate(signals, ctx())
    (req,) = result.requests
    assert req.quantity * D("100.10") <= D("100")  # 10% symbol cap shared, never 160
    assert set(req.attribution) == {"orb_v1", "vwap_v1"}
    assert sum(req.attribution.values()) == req.quantity


def test_different_symbols_stay_separate_orders() -> None:
    result = alloc.allocate([signal(symbol="SPY"), signal(symbol="QQQ")], ctx())
    assert sorted(r.symbol for r in result.requests) == ["QQQ", "SPY"]


# ---- end-of-day flatten ---------------------------------------------------------------------------


def test_flatten_sells_every_holder_and_attributes_to_them() -> None:
    held = (hold("orb_v1", "0.5"), hold("vwap_v1", "0.25"))
    flat = signal("eod_flatten", side=Side.SELL, notional="75", limit="99.00")
    (req,) = alloc.allocate([flat], ctx(holdings=held, held_qty={"SPY": D("0.75")})).requests
    assert (req.side, req.quantity) == (Side.SELL, D("0.75"))
    assert req.attribution == {"orb_v1": D("-0.5"), "vwap_v1": D("-0.25")}
    assert req.strategy_ids == ("orb_v1", "vwap_v1")


def test_flatten_attributes_unowned_shares_to_the_flatten_strategy() -> None:
    held = (hold("orb_v1", "0.5"),)
    flat = signal("eod_flatten", side=Side.SELL, notional="100", limit="99.00")
    (req,) = alloc.allocate(
        [flat], ctx(holdings=held, held_qty={"SPY": D("0.8")})
    ).requests  # 0.3 has no owner
    assert req.quantity == D("0.8")
    assert req.attribution == {"orb_v1": D("-0.5"), "eod_flatten": D("-0.3")}


def test_flatten_respects_shares_already_being_sold() -> None:
    held = (hold("orb_v1", "0.5"),)
    flat = signal("eod_flatten", side=Side.SELL, limit="99.00")
    result = alloc.allocate(
        [flat], ctx(holdings=held, held_qty={"SPY": D("0.5")}, open_sell_qty={"SPY": D("0.5")})
    )
    assert result.requests == ()


# ---- invariants -----------------------------------------------------------------------------------


def test_signal_ids_make_the_dedupe_key_stable_and_unique() -> None:
    a, b = signal(), signal(symbol="QQQ")
    one = alloc.allocate([a, b], ctx())
    two = alloc.allocate([b, a], ctx())
    assert sorted(r.dedupe_key for r in one.requests) == sorted(r.dedupe_key for r in two.requests)
    assert len({r.dedupe_key for r in one.requests}) == 2


def test_allocation_is_deterministic_regardless_of_signal_order() -> None:
    held = (hold("vwap_v1", "0.3"),)
    signals = [signal("orb_v1"), signal("vwap_v1", side=Side.SELL, limit="99.80")]
    first = alloc.allocate(signals, ctx(holdings=held, held_qty={"SPY": D("0.3")}))
    second = alloc.allocate(list(reversed(signals)), ctx(holdings=held, held_qty={"SPY": D("0.3")}))
    assert first.requests == second.requests


def test_every_signal_gets_exactly_one_note() -> None:
    signals = [signal(), signal("vwap_v1", symbol="QQQ"), signal(symbol="ZZZ")]
    result = alloc.allocate(signals, ctx())
    assert len(result.notes) == len(signals)
    assert {n.signal_id for n in result.notes} == {s.signal_id for s in signals}


def test_targets_summarise_the_decision() -> None:
    (req,) = alloc.allocate([signal()], ctx()).requests
    result = alloc.allocate([signal()], ctx())
    assert result.targets["SPY"]["net_qty"] == str(req.quantity)
    assert result.targets["SPY"]["side"] == "BUY"


@pytest.mark.parametrize("notional", ["0.01", "1", "33.33", "79.99", "80"])
def test_notional_never_exceeds_the_target_for_any_size(notional: str) -> None:
    result = alloc.allocate([signal(notional=notional)], ctx())
    for req in result.requests:
        assert req.quantity * (req.limit_price or D(0)) <= D(notional)
