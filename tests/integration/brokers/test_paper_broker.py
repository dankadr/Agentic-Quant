from datetime import timedelta
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
from trading.core.enums import OrderType, Side, TimeInForce
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

    def _make(faults: FaultPlan = NO_FAULTS) -> PaperBroker:
        broker = PaperBroker(
            session_factory, account_ref=ACCOUNT, config=CFG.paper_broker,
            instruments=instruments, clock=clock, faults=faults,
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
