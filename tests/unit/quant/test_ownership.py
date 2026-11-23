from dataclasses import replace

import pytest

from tests.unit.quant.test_allocator import D, alloc, ctx, hold, signal
from trading.core.enums import PositionEffect, Side


def test_simultaneous_entries_have_one_owner_regardless_of_input_order() -> None:
    signals = [signal("vwap_v1"), signal("orb_v1")]
    for candidates in (signals, list(reversed(signals))):
        result = alloc.allocate(candidates, ctx())
        (request,) = result.requests
        assert request.strategy_ids == ("orb_v1",)
        assert request.attribution == {"orb_v1": D("0.7992")}
        assert ("vwap_v1", "SYMBOL_OWNED") in {
            (note.strategy_id, note.reason) for note in result.notes
        }


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_pending_entry_or_exit_reserves_ticker_for_its_owner(side: str) -> None:
    result = alloc.allocate(
        [signal("vwap_v1")],
        ctx(in_flight=frozenset({("orb_v1", "SPY", side)})),
    )
    assert result.requests == ()
    assert result.notes[0].reason == "SYMBOL_OWNED"


def test_existing_owner_can_add_but_other_strategy_cannot() -> None:
    result = alloc.allocate(
        [signal("orb_v1", notional="10"), signal("vwap_v1", notional="10")],
        ctx(holdings=(hold("vwap_v1", "0.3"),), held_qty={"SPY": D("0.3")}),
    )
    (request,) = result.requests
    assert request.strategy_ids == ("vwap_v1",)
    assert request.quantity == D("0.0999")


def test_exit_cannot_transfer_ownership_before_it_fills() -> None:
    result = alloc.allocate(
        [signal("orb_v1"), signal("vwap_v1", side=Side.SELL)],
        ctx(holdings=(hold("vwap_v1", "0.3"),), held_qty={"SPY": D("0.3")}),
    )
    (request,) = result.requests
    assert (request.side, request.quantity) == (Side.SELL, D("0.3"))
    assert request.attribution == {"vwap_v1": D("-0.3")}


def test_unattributed_position_blocks_entries() -> None:
    result = alloc.allocate([signal()], ctx(held_qty={"SPY": D("0.1")}))
    assert result.requests == ()
    assert result.notes[0].reason == "SYMBOL_OWNED"


def test_legacy_shared_holdings_can_exit_but_cannot_grow() -> None:
    holdings = (hold("orb_v1", "0.2"), hold("vwap_v1", "0.3"))
    context = ctx(holdings=holdings, held_qty={"SPY": D("0.5")})
    result = alloc.allocate([signal("orb_v1"), signal("vwap_v1")], context)
    assert result.requests == ()
    result = alloc.allocate([signal("orb_v1", side=Side.SELL)], context)
    assert result.requests[0].attribution == {"orb_v1": D("-0.2")}


def test_unsized_candidate_does_not_claim_ticker() -> None:
    context = replace(ctx(), entry_scale={"orb_v1": D(0)})
    result = alloc.allocate([signal("orb_v1"), signal("vwap_v1")], context)
    assert result.requests[0].strategy_ids == ("vwap_v1",)


def test_new_strategy_can_enter_after_position_and_orders_are_gone() -> None:
    assert alloc.allocate([signal("vwap_v1")], ctx()).requests[0].strategy_ids == ("vwap_v1",)


@pytest.mark.parametrize("owners", [{}, {"SPY": frozenset({"vwap_v1"})}])
def test_authoritative_ownership_is_not_overridden_by_stale_analytics(
    owners: dict[str, frozenset[str]],
) -> None:
    result = alloc.allocate(
        [signal("vwap_v1")],
        ctx(symbol_owners=owners, holdings=(hold("orb_v1", "0.1"),)),
    )
    assert result.requests[0].strategy_ids == ("vwap_v1",)


def test_simultaneous_short_entries_have_one_owner() -> None:
    signals = [
        signal(strategy).model_copy(
            update={"side": Side.SELL, "position_effect": PositionEffect.OPEN_SHORT}
        )
        for strategy in ("vwap_v1", "orb_v1")
    ]
    result = alloc.allocate(signals, ctx(allow_short=True, asks={"SPY": D("100.1")}))
    assert result.requests[0].strategy_ids == ("orb_v1",)
    assert result.requests[0].position_effect is PositionEffect.OPEN_SHORT


def test_existing_short_blocks_foreign_short_but_owner_can_cover() -> None:
    foreign = signal("vwap_v1").model_copy(
        update={
            "side": Side.SELL,
            "position_effect": PositionEffect.OPEN_SHORT,
        }
    )
    cover = signal("orb_v1").model_copy(update={"position_effect": PositionEffect.CLOSE_SHORT})
    result = alloc.allocate(
        [foreign, cover],
        ctx(
            allow_short=True,
            holdings=(hold("orb_v1", "-0.3"),),
            held_qty={"SPY": D("-0.3")},
            asks={"SPY": D("100.1")},
        ),
    )
    assert len(result.requests) == 1
    assert result.requests[0].position_effect is PositionEffect.CLOSE_SHORT
    assert result.requests[0].attribution == {"orb_v1": D("0.3")}
