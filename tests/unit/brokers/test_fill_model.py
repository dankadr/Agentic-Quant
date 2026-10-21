from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from trading.brokers.paper.fill_model import VenueOrderView, estimate_fees, evaluate_fill
from trading.core.config import PaperBrokerConfig, load_config
from trading.core.enums import AssetType, OrderType, Side
from trading.data.instruments import InstrumentInfo
from trading.data.models import Bar, Quote

CFG = load_config(Path("configs")).paper_broker
T = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
D = Decimal


def inst(fractional: bool = True) -> InstrumentInfo:
    return InstrumentInfo(id=uuid4(), symbol="SPY", asset_type=AssetType.ETF, tick_size=D("0.01"),
                          lot_size=D(1), fractional_eligible=fractional, sector="Broad Market")  # fmt: skip


def order(
    side: Side = Side.BUY,
    otype: OrderType = OrderType.LIMIT,
    limit: str | None = "100.05",
    qty: str = "0.5",
    filled: str = "0",
    seq: int = 0,
    eligible: datetime = T,
) -> VenueOrderView:
    return VenueOrderView(
        client_order_id="c1", symbol="SPY", side=side, order_type=otype,
        limit_price=D(limit) if limit else None, qty=D(qty), filled_qty=D(filled),
        eligible_at=eligible, fill_seq=seq,
    )  # fmt: skip


def quote(
    bid: str = "99.99",
    ask: str = "100.00",
    ts: datetime = T,
    bs: str | None = None,
    asz: str | None = None,
) -> Quote:
    return Quote(symbol="SPY", ts=ts, bid=D(bid), ask=D(ask), bid_size=D(bs) if bs else None,
                 ask_size=D(asz) if asz else None, provider="p")  # fmt: skip


def bar(volume: str = "1000", low: str = "99", high: str = "101") -> Bar:
    return Bar(symbol="SPY", interval="1m", ts=T - timedelta(minutes=1), open=D("100"), high=D(high),
               low=D(low), close=D("100"), volume=D(volume), provider="p", source_seq=None)  # fmt: skip


NOW = T + timedelta(seconds=1)


def fill(
    o: VenueOrderView,
    q: Quote | None = None,
    b: Bar | None = None,
    now: datetime = NOW,
    instrument: InstrumentInfo | None = None,
    cfg: PaperBrokerConfig = CFG,
):  # type: ignore[no-untyped-def]
    return evaluate_fill(o, quote=q if q is not None else quote(), bar=b if b is not None else bar(),
                         now=now, instrument=instrument or inst(), cfg=cfg)  # fmt: skip


def test_buy_limit_fills_at_ask_plus_slippage() -> None:
    d = fill(order())
    assert d is not None
    assert (d.price, d.qty) == (D("100.02"), D("0.5"))
    assert d.execution_id == "c1-1"
    assert d.slippage_estimate == D("0.0125")  # |100.02 - 99.995| * 0.5
    assert d.fees == D("0.0000")


def test_buy_price_rounds_up_to_tick() -> None:
    d = fill(order(limit="101"), q=quote(bid="100.00", ask="100.01"))
    assert d is not None
    assert d.price == D("100.04")


def test_limit_not_reached_no_fill() -> None:
    assert fill(order(limit="100.01")) is None
    assert fill(order(limit="100.02")) is not None  # exactly at the limit still fills


def test_sell_price_rounds_down_and_respects_limit() -> None:
    d = fill(order(side=Side.SELL, limit="99.00"))
    assert d is not None
    assert d.price == D("99.97")  # 99.99 * 0.9998 = 99.970002 -> floor to tick
    assert fill(order(side=Side.SELL, limit="99.97")) is not None  # exactly at the limit fills
    assert fill(order(side=Side.SELL, limit="99.98")) is None


def test_market_order_ignores_limit_but_not_liquidity() -> None:
    d = fill(order(otype=OrderType.MARKET, limit=None))
    assert d is not None
    assert d.price == D("100.02")
    assert fill(order(otype=OrderType.MARKET, limit=None), b=bar(volume="0")) is None


def test_no_fill_before_latency() -> None:
    late = T + timedelta(milliseconds=500)
    assert fill(order(eligible=late), now=T + timedelta(milliseconds=499)) is None
    assert (
        fill(order(eligible=late), q=quote(ts=T + timedelta(milliseconds=450)), now=late)
        is not None
    )


@pytest.mark.parametrize(
    "bad",
    ["missing", "stale_6s", "crossed", "zero_bid"],
)
def test_no_fill_on_bad_quote(bad: str) -> None:
    quotes = {
        "stale_6s": quote(ts=NOW - timedelta(seconds=6)),
        "crossed": quote(bid="100.05", ask="100.00"),
        "zero_bid": quote(bid="0", ask="100.00"),
    }
    o = order()
    if bad == "missing":
        assert evaluate_fill(o, quote=None, bar=bar(), now=NOW, instrument=inst(), cfg=CFG) is None
    else:
        assert fill(o, q=quotes[bad]) is None


def test_quote_exactly_at_max_age_is_usable() -> None:
    assert fill(order(), q=quote(ts=NOW - timedelta(seconds=5))) is not None


def test_no_fill_without_bar_or_volume() -> None:
    o = order()
    assert evaluate_fill(o, quote=quote(), bar=None, now=NOW, instrument=inst(), cfg=CFG) is None
    assert fill(o, b=bar(volume="0")) is None


def test_partial_fill_capped_by_participation_then_remainder() -> None:
    first = fill(order(qty="10", limit="101"), b=bar(volume="100"))
    assert first is not None
    assert first.qty == D("5")  # 100 * 0.05
    second = fill(order(qty="10", limit="101", filled="5", seq=1), b=bar(volume="100"))
    assert second is not None
    assert (second.qty, second.execution_id) == (D("5"), "c1-2")
    assert fill(order(qty="10", limit="101", filled="10", seq=2), b=bar(volume="100")) is None


def test_displayed_size_caps_fill() -> None:
    buy = fill(order(qty="10", limit="101"), q=quote(asz="3"), b=bar(volume="100000"))
    sell = fill(order(side=Side.SELL, qty="10", limit="1"), q=quote(bs="2"), b=bar(volume="100000"))
    assert buy is not None
    assert sell is not None
    assert (buy.qty, sell.qty) == (D("3"), D("2"))


def test_whole_share_instrument_floors_to_lot() -> None:
    d = fill(order(qty="10", limit="101"), b=bar(volume="54"), instrument=inst(fractional=False))
    assert d is not None
    assert d.qty == D("2")  # 54 * 0.05 = 2.7 -> 2


def test_dust_remainder_below_increment_does_not_fill() -> None:
    assert fill(order(qty="0.00005")) is None


def test_never_fills_at_bar_extremes() -> None:
    d = fill(order(), b=bar(low="95", high="105"))
    assert d is not None
    assert d.price == D("100.02")


def test_sell_fees_and_buy_has_none() -> None:
    assert estimate_fees(Side.SELL, D("10"), D("100"), CFG) == D("0.0295")
    assert estimate_fees(Side.BUY, D("10"), D("100"), CFG) == D("0.0000")


def test_taf_is_capped_per_trade() -> None:
    fees = estimate_fees(Side.SELL, D("1000000"), D("1"), CFG)
    assert fees == D("1000000") * D("1") * CFG.sell_fee_rate + CFG.taf_max_per_trade_usd


def test_commission_added_to_every_order() -> None:
    cfg = CFG.model_copy(update={"commission_per_order_usd": D("0.50")})
    assert estimate_fees(Side.BUY, D("1"), D("100"), cfg) == D("0.5000")


def test_deterministic() -> None:
    assert fill(order()) == fill(order())
