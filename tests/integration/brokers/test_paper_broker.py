from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.base import (
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.brokers.paper.broker import PaperBroker
from trading.brokers.paper.faults import FaultPlan
from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.core.enums import OrderType, PositionEffect, Side, TimeInForce
from trading.data.instruments import upsert_universe
from trading.data.models import Bar, Quote

CFG = load_config(Path("configs"))
D = Decimal
ACCOUNT = "paper-primary"
NO_FAULTS = FaultPlan()


def request(
    coid: str = "c1",
    side: Side = Side.BUY,
    qty: str = "0.5",
    limit: str | None = "100.05",
    symbol: str = "SPY",
    otype: OrderType = OrderType.LIMIT,
) -> BrokerOrderRequest:
    return BrokerOrderRequest(
        client_order_id=coid, symbol=symbol, side=side, order_type=otype, qty=D(qty),
        limit_price=D(limit) if limit else None, time_in_force=TimeInForce.DAY,
    )  # fmt: skip


@pytest.fixture
def make_broker(db: Session, session_factory: sessionmaker[Session], clock: ManualClock):  # type: ignore[no-untyped-def]
    instruments = upsert_universe(db, CFG.universe)
    db.commit()

    def _make(
        faults: FaultPlan = NO_FAULTS,
        *,
        allow_short: bool = False,
        latency_ms: int = CFG.paper_broker.latency_ms,
    ) -> PaperBroker:
        broker = PaperBroker(
            session_factory, account_ref=ACCOUNT, config=CFG.paper_broker.model_copy(update={"latency_ms": latency_ms}),
            instruments=instruments, clock=clock, faults=faults, allow_short=allow_short,
        )  # fmt: skip
        broker.open_account(D("1000.00"))
        return broker

    return _make


def tick(
    broker: PaperBroker,
    clock: ManualClock,
    *,
    bid: str = "99.99",
    ask: str = "100.00",
    volume: str = "1000",
    symbol: str = "SPY",
    advance: float = 1.0,
):  # type: ignore[no-untyped-def]
    clock.advance(timedelta(seconds=advance))
    now = clock.now()
    q = Quote(
        symbol=symbol, ts=now, bid=D(bid), ask=D(ask), bid_size=None, ask_size=None, provider="p"
    )
    b = Bar(symbol=symbol, interval="1m", ts=now - timedelta(minutes=1), open=D("100"), high=D("101"),
            low=D("99"), close=D("100"), volume=D(volume), provider="p", source_seq=None)  # fmt: skip
    return broker.process_tick(quotes={symbol: q}, bars={symbol: b})


def test_submit_then_fill_on_tick(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    state = broker.submit_order(request())
    assert state.status is BrokerOrderStatus.ACCEPTED
    assert state.broker_order_id
    fills = tick(broker, clock)
    assert [(f.execution_id, f.qty, f.price) for f in fills] == [("c1-1", D("0.5"), D("100.02"))]
    done = broker.get_order("c1")
    assert done is not None
    assert (done.status, done.filled_qty, done.avg_fill_price) == (
        BrokerOrderStatus.FILLED, D("0.5"), D("100.02"),
    )  # fmt: skip
    account = broker.get_account()
    assert account.cash == D("949.9900")
    assert dict(account.positions) == {"SPY": D("0.5")}


def test_no_fill_before_latency_elapsed(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request())
    assert tick(broker, clock, advance=0.2) == []
    assert len(tick(broker, clock, advance=0.4)) == 1


def test_limit_not_reached_stays_open(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request(limit="99.00"))
    assert tick(broker, clock) == []
    assert [o.client_order_id for o in broker.list_open_orders()] == ["c1"]


def test_partial_fills_across_ticks_then_filled(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request(qty="8", limit="101"))
    first = tick(broker, clock, volume="100")  # participation 5% -> 5 shares
    assert [(f.execution_id, f.qty) for f in first] == [("c1-1", D("5"))]
    mid = broker.get_order("c1")
    assert mid is not None
    assert (mid.status, mid.filled_qty) == (BrokerOrderStatus.PARTIALLY_FILLED, D("5"))
    second = tick(broker, clock, volume="100")
    assert [(f.execution_id, f.qty) for f in second] == [("c1-2", D("3"))]
    last = broker.get_order("c1")
    assert last is not None
    assert last.status is BrokerOrderStatus.FILLED
    assert broker.list_open_orders() == []


def test_execution_ids_continue_across_fresh_broker_instances(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    a = make_broker()
    a.submit_order(request(qty="8", limit="101"))
    tick(a, clock, volume="100")
    b = make_broker()  # simulates a restart: no in-memory state carried over
    second = tick(b, clock, volume="100")
    assert [f.execution_id for f in second] == ["c1-2"]


def test_cancel_partially_filled_keeps_fills(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request(qty="8", limit="101"))
    tick(broker, clock, volume="100")
    canceled = broker.cancel_order("c1")
    assert (canceled.status, canceled.filled_qty) == (BrokerOrderStatus.CANCELED, D("5"))
    assert len(broker.list_fills()) == 1
    assert tick(broker, clock, volume="100") == []
    assert broker.cancel_order("c1").status is BrokerOrderStatus.CANCELED  # idempotent


def test_cancel_unknown_order_rejected(make_broker) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(BrokerRejected, match="UNKNOWN_ORDER"):
        make_broker().cancel_order("nope")


def test_day_orders_expire_at_close(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request(limit="99.00"))
    assert broker.expire_day_orders(session_close=clock.now() + timedelta(hours=6)) == 1
    expired = broker.get_order("c1")
    assert expired is not None
    assert expired.status is BrokerOrderStatus.EXPIRED
    assert broker.expire_day_orders(session_close=clock.now() + timedelta(hours=6)) == 0


def test_timeout_after_accept_order_exists(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker(FaultPlan(timeout_after_accept=frozenset({"c1"})))
    with pytest.raises(BrokerTimeout):
        broker.submit_order(request())
    seen = make_broker().get_order("c1")
    assert seen is not None
    assert seen.status is BrokerOrderStatus.ACCEPTED


def test_timeout_before_accept_order_absent(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker(FaultPlan(timeout_before_accept=frozenset({"c1"})))
    with pytest.raises(BrokerTimeout):
        broker.submit_order(request())
    assert make_broker().get_order("c1") is None


def test_unavailable_raises_everywhere(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker(FaultPlan(unavailable=True))
    for call in (
        lambda: broker.submit_order(request()),
        lambda: broker.get_order("c1"),
        broker.list_fills,
        broker.get_account,
        broker.list_open_orders,
        lambda: broker.cancel_order("c1"),
    ):
        with pytest.raises(BrokerUnavailable):
            call()


def test_duplicate_client_order_id_rejected(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request())
    with pytest.raises(BrokerRejected, match="DUPLICATE_CLIENT_ORDER_ID"):
        broker.submit_order(request())
    assert len(broker.list_open_orders()) == 1


def test_insufficient_funds_rejected_and_reservations_count(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    with pytest.raises(BrokerRejected, match="INSUFFICIENT_FUNDS"):
        broker.submit_order(request(qty="20", limit="100.00"))
    broker.submit_order(request("c1", qty="9", limit="100.00"))
    with pytest.raises(BrokerRejected, match="INSUFFICIENT_FUNDS"):
        broker.submit_order(request("c2", qty="2", limit="100.00"))


def test_sell_requires_position_and_counts_open_sells(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    with pytest.raises(BrokerRejected, match="INSUFFICIENT_POSITION"):
        broker.submit_order(request("s0", side=Side.SELL, qty="1", limit="99"))
    broker.submit_order(request("b1", qty="1", limit="101"))
    tick(broker, clock)
    broker.submit_order(request("s1", side=Side.SELL, qty="0.6", limit="50"))
    with pytest.raises(BrokerRejected, match="INSUFFICIENT_POSITION"):
        broker.submit_order(request("s2", side=Side.SELL, qty="0.5", limit="50"))


def test_unknown_symbol_tick_and_quantity_rejected(make_broker) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    with pytest.raises(BrokerRejected, match="UNKNOWN_SYMBOL"):
        broker.submit_order(request(symbol="NOPE"))
    with pytest.raises(BrokerRejected, match="INVALID_TICK_SIZE"):
        broker.submit_order(request(limit="100.005"))
    with pytest.raises(BrokerRejected, match="INVALID_QUANTITY"):
        broker.submit_order(request(qty="0.00005"))


def test_market_buy_needs_reference_price(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    market = request("m1", otype=OrderType.MARKET, limit=None, qty="1")
    with pytest.raises(BrokerRejected, match="NO_REFERENCE_PRICE"):
        broker.submit_order(market)
    tick(broker, clock)  # learns the latest ask
    assert broker.submit_order(market).status is BrokerOrderStatus.ACCEPTED


def test_account_state_after_round_trip_includes_fees(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request("b1", qty="1", limit="101"))
    tick(broker, clock)  # buys 1 @ 100.02
    broker.submit_order(request("s1", side=Side.SELL, qty="1", limit="50"))
    tick(broker, clock, bid="101.00", ask="101.02")  # sells 1 @ 100.97
    account = broker.get_account()
    # 1000 - 100.02 + 100.9798 -> sell price 101.00 * 0.9998 = 100.9798 -> floor tick 100.97
    fees = broker.list_fills()[1].fees
    assert fees > 0
    assert account.cash == D("1000") - D("100.02") + D("100.97") - fees
    assert dict(account.positions) == {}


def test_list_fills_ordered_and_since_filter(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request("b1", qty="0.2", limit="101"))
    tick(broker, clock)
    cutoff = clock.now() + timedelta(seconds=1)
    broker.submit_order(request("b2", qty="0.2", limit="101"))
    tick(broker, clock, advance=2)
    assert [f.client_order_id for f in broker.list_fills()] == ["b1", "b2"]
    assert [f.client_order_id for f in broker.list_fills(since=cutoff)] == ["b2"]
    assert all(f.broker_order_id for f in broker.list_fills())


def test_venue_state_is_independent_of_application_tables(make_broker, clock, db: Session) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.submit_order(request())
    tick(broker, clock)
    for table in ("orders", "fills", "cash_ledger", "positions", "order_intents"):
        assert db.execute(text(f"select count(*) from {table}")).scalar_one() == 0


def test_open_account_is_idempotent(make_broker, db: Session) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker()
    broker.open_account(D("5000.00"))  # must not change existing starting cash
    assert broker.get_account().cash == D("1000.0000")


def test_short_venue_reserves_entries_and_allows_disabled_cover(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    sell = replace(
        request("short", Side.SELL, "6", "99"), position_effect=PositionEffect.OPEN_SHORT
    )
    disabled = make_broker()
    with pytest.raises(BrokerRejected):
        disabled.submit_order(sell)
    enabled = make_broker(allow_short=True)
    tick(enabled, clock)
    enabled.submit_order(sell)
    with pytest.raises(BrokerRejected):
        enabled.submit_order(replace(sell, client_order_id="additional", qty=D("5")))
    assert tick(enabled, clock)
    with pytest.raises(BrokerRejected):
        enabled.submit_order(request("flip", qty="1"))
    cover = replace(
        request("cover", qty="4", limit="101"), position_effect=PositionEffect.CLOSE_SHORT
    )
    disabled.submit_order(cover)
    with pytest.raises(BrokerRejected):
        disabled.submit_order(replace(cover, client_order_id="overcover"))
    assert tick(disabled, clock)
    assert disabled.get_account().positions["SPY"] == -2


def test_adverse_ask_prevents_short_fill(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker(allow_short=True)
    tick(broker, clock)
    sell = replace(
        request("adverse", Side.SELL, "8", "99"), position_effect=PositionEffect.OPEN_SHORT
    )
    broker.submit_order(sell)
    assert tick(broker, clock, bid="130", ask="131") == []
    assert broker.get_account().positions == {}
    assert broker.get_order("adverse").filled_qty == 0


def test_stale_ask_for_other_short_blocks_new_entry(make_broker, clock) -> None:  # type: ignore[no-untyped-def]
    broker = make_broker(allow_short=True)
    tick(broker, clock)
    broker.submit_order(
        replace(request("s", Side.SELL, "1", "99"), position_effect=PositionEffect.OPEN_SHORT)
    )
    assert tick(broker, clock)
    clock.advance(timedelta(seconds=CFG.paper_broker.quote_max_age_seconds + 1))
    with pytest.raises(BrokerRejected, match="NO_REFERENCE_PRICE"):
        broker.submit_order(request("other", symbol="QQQ"))


def test_long_sale_proceeds_fund_shorts_only_after_holiday_settlement(make_broker, clock) -> None:
    broker = make_broker(allow_short=True)
    clock.set(datetime(2026, 11, 25, 16, tzinfo=UTC))
    broker.submit_order(request("buy", qty="9"))
    assert tick(broker, clock)
    broker.submit_order(request("sell", Side.SELL, "9", "99"))
    assert tick(broker, clock)
    assert broker.get_account().cash > 990
    short = replace(
        request("short", Side.SELL, "5", "99"), position_effect=PositionEffect.OPEN_SHORT
    )
    for day in (25, 26):
        clock.set(datetime(2026, 11, day, 17, tzinfo=UTC))
        tick(broker, clock)
        with pytest.raises(BrokerRejected, match="INSUFFICIENT_FUNDS"):
            broker.submit_order(short)
    clock.set(datetime(2026, 11, 27, 16, tzinfo=UTC))
    tick(broker, clock)
    broker.submit_order(short)
    assert tick(broker, clock)


def test_adverse_fill_cannot_use_other_symbols_unsettled_sale(make_broker, clock) -> None:
    broker = make_broker(allow_short=True)
    broker.submit_order(request("buy", qty="8"))
    assert tick(broker, clock)
    tick(broker, clock, symbol="QQQ")
    short = replace(
        request("short", Side.SELL, "1.5", "99", symbol="QQQ"),
        position_effect=PositionEffect.OPEN_SHORT,
    )
    broker.submit_order(short)
    broker.submit_order(request("sell", Side.SELL, "8", "99"))
    assert tick(broker, clock)
    assert tick(broker, clock, symbol="QQQ", bid="150", ask="151") == []
    assert broker.get_account().positions == {}


def test_restart_recovers_same_time_open_and_cover_in_venue_sequence(
    make_broker, clock, db
) -> None:
    from tests.integration.portfolio.helpers import DAY, new_order, open_account
    from trading.portfolio.ledger import apply_fill

    broker = make_broker(allow_short=True, latency_ms=0)
    tick(broker, clock)
    short = replace(
        request("z-open", Side.SELL, "2", "99"), position_effect=PositionEffect.OPEN_SHORT
    )
    cover = replace(request("a-cover", qty="2"), position_effect=PositionEffect.CLOSE_SHORT)
    for order in (short, cover):
        broker.submit_order(order)
        assert tick(broker, clock, advance=0)
    restarted = make_broker()
    fills = restarted.list_fills(since=clock.now())
    assert [f.client_order_id for f in fills] == ["z-open", "a-cover"]
    account = open_account(db)
    for req in (short, cover):
        row = new_order(db, account, req.client_order_id, side=req.side.value, qty="2")
        row.position_effect = req.position_effect.value
    for fill in fills:
        result = apply_fill(db, account_id=account, fill=fill, settles_on=DAY, now=clock.now())
    assert result.position_qty == 0


def test_cover_can_reduce_short_even_when_cash_cannot_pay_entire_loss(make_broker, clock) -> None:
    broker = make_broker(allow_short=True)
    tick(broker, clock)
    broker.submit_order(
        replace(request("s", Side.SELL, "8", "99"), position_effect=PositionEffect.OPEN_SHORT)
    )
    assert tick(broker, clock)
    disabled = make_broker()
    cover = replace(request("c", qty="8", limit="260"), position_effect=PositionEffect.CLOSE_SHORT)
    disabled.submit_order(cover)
    assert tick(disabled, clock, bid="250", ask="251")
    state = disabled.get_account()
    assert not state.positions
    assert state.cash < 0
