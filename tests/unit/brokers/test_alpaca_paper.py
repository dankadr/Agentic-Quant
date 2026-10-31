"""The Alpaca PAPER adapter, against recorded-shape responses. No network, no credentials."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from trading.brokers.alpaca.adapter import PAPER_BASE_URL, AlpacaConfigError, AlpacaPaperAdapter
from trading.brokers.alpaca.events import new_fills, parse_trade_update
from trading.brokers.alpaca.gate import capability_problems, read_only_gate
from trading.brokers.base import (
    BrokerOrderRequest,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.brokers.capital import deployable_cash, risk_equity, strategy_equity
from trading.core.enums import OrderType, Side, TimeInForce

NOW = datetime(2026, 10, 29, 14, 0, tzinfo=UTC)


def _order(
    coid: str, status: str = "new", created: str = "2026-10-29T14:00:00Z", **extra: object
) -> dict:  # type: ignore[type-arg]
    base = {
        "id": f"broker-{coid}",
        "client_order_id": coid,
        "status": status,
        "qty": "0.5",
        "filled_qty": "0",
        "filled_avg_price": None,
        "symbol": "SPY",
        "side": "buy",
        "created_at": created,
        "updated_at": created,
    }
    base.update(extra)
    return base


def _adapter(handler, base_url: str = PAPER_BASE_URL) -> AlpacaPaperAdapter:  # type: ignore[no-untyped-def]
    return AlpacaPaperAdapter(
        key_id="PK-test",
        secret="secret-test",
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )


def _req(coid: str = "paper-orb-1", **overrides: object) -> BrokerOrderRequest:
    base: dict[str, object] = dict(
        client_order_id=coid,
        symbol="SPY",
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal("0.5"),
        limit_price=Decimal("100.05"),
        time_in_force=TimeInForce.DAY,
    )
    base.update(overrides)
    return BrokerOrderRequest(**base)  # type: ignore[arg-type]


def test_only_the_paper_host_is_accepted() -> None:
    with pytest.raises(AlpacaConfigError):
        AlpacaPaperAdapter(key_id="k", secret="s", base_url="https://api.alpaca.markets")


def test_submission_sends_the_client_order_id_and_the_paper_credentials() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_order("paper-orb-1"))

    state = _adapter(handler).submit_order(_req())
    (request,) = seen
    assert request.url.host == "paper-api.alpaca.markets"
    assert request.headers["APCA-API-KEY-ID"] == "PK-test"
    body = json.loads(request.content)
    assert body == {
        "symbol": "SPY",
        "qty": "0.5",
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "100.05",
        "client_order_id": "paper-orb-1",
    }
    assert state.status is BrokerOrderStatus.ACCEPTED


def test_a_timeout_is_reported_and_the_order_is_never_resubmitted() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    with pytest.raises(BrokerTimeout):
        _adapter(handler).submit_order(_req())
    assert calls["n"] == 1  # reconciliation, not retry, decides what happened


def test_server_errors_are_unavailable_and_refusals_are_rejections() -> None:
    adapter = _adapter(lambda r: httpx.Response(503))
    with pytest.raises(BrokerUnavailable):
        adapter.submit_order(_req())
    adapter = _adapter(lambda r: httpx.Response(422, json={"message": "insufficient buying power"}))
    with pytest.raises(BrokerRejected, match="HTTP_422"):
        adapter.submit_order(_req())


def test_redirects_are_refused_not_followed() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(307, headers={"location": "https://api.alpaca.markets/v2/orders"})

    with pytest.raises(BrokerUnavailable, match="REDIRECT_REFUSED"):
        _adapter(handler).submit_order(_req())
    assert calls["n"] == 1


def test_an_old_submission_is_found_by_paging_back_through_history() -> None:
    """Missing an old order would make reconciliation think it never existed."""
    pages = [
        [_order(f"x-{i}", created=f"2026-10-29T13:{i % 60:02d}:00Z") for i in range(500)],
        [_order("paper-orb-old", created="2026-10-20T13:30:00Z")],
    ]
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=pages[min(calls["n"], 2) - 1])

    found = _adapter(handler).get_order("paper-orb-old")
    assert found is not None and found.client_order_id == "paper-orb-old"
    assert calls["n"] == 2


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("new", BrokerOrderStatus.ACCEPTED),
        ("partially_filled", BrokerOrderStatus.PARTIALLY_FILLED),
        ("filled", BrokerOrderStatus.FILLED),
        ("canceled", BrokerOrderStatus.CANCELED),
        ("expired", BrokerOrderStatus.EXPIRED),
        ("rejected", BrokerOrderStatus.REJECTED),
    ],
)
def test_known_statuses_map_to_internal_statuses(raw: str, expected: BrokerOrderStatus) -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json=[_order("c1", status=raw)]))
    state = adapter.get_order("c1")
    assert state is not None and state.status is expected


def test_an_unknown_status_is_never_guessed() -> None:
    adapter = _adapter(lambda r: httpx.Response(200, json=[_order("c1", status="mystery")]))
    with pytest.raises(BrokerTimeout, match="UNMAPPED_STATUS"):
        adapter.get_order("c1")


def test_account_and_positions_are_read() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"cash": "1000.00", "account_number": "PA123"})
        return httpx.Response(200, json=[{"symbol": "SPY", "qty": "0.5"}])

    account = _adapter(handler).get_account()
    assert (account.cash, account.positions) == (Decimal("1000.00"), {"SPY": Decimal("0.5")})


def test_fills_come_back_in_time_order_with_their_execution_ids() -> None:
    activities = [
        {
            "id": "b",
            "order_id": "o2",
            "client_order_id": "c2",
            "symbol": "SPY",
            "side": "sell",
            "qty": "1",
            "price": "101",
            "transaction_time": "2026-10-29T15:00:00Z",
        },
        {
            "id": "a",
            "order_id": "o1",
            "client_order_id": "c1",
            "symbol": "SPY",
            "side": "buy",
            "qty": "1",
            "price": "100",
            "transaction_time": "2026-10-29T14:00:00Z",
        },
    ]
    fills = _adapter(lambda r: httpx.Response(200, json=activities)).list_fills()
    assert [f.execution_id for f in fills] == ["a", "b"]
    assert fills[0].side is Side.BUY and fills[0].fees == Decimal(0)


def test_a_fractional_order_needs_a_fractionable_asset_and_verified_fractional_limits() -> None:
    fractional = _req(qty=Decimal("0.5"))
    no_fraction = type("A", (), {"tradable": True, "fractionable": False, "symbol": "X"})()
    assert "FRACTIONAL_NOT_SUPPORTED" in capability_problems(
        fractional, no_fraction, fractional_limit_verified=True
    )
    fractionable = type("A", (), {"tradable": True, "fractionable": True, "symbol": "X"})()
    assert capability_problems(fractional, fractionable, fractional_limit_verified=False) == [
        "FRACTIONAL_LIMIT_NOT_VERIFIED"
    ]
    assert (
        capability_problems(_req(qty=Decimal(1)), fractionable, fractional_limit_verified=False)
        == []
    )


def test_the_gate_lists_every_reason_it_refuses() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"cash": "1000", "account_number": "OTHER"})
        if req.url.path == "/v2/positions":
            return httpx.Response(200, json=[{"symbol": "SPY", "qty": "2"}])
        return httpx.Response(200, json=[_order("c1", status="new")])

    result = read_only_gate(
        _adapter(handler), enabled=False, expected_account="PA123", require_flat=True
    )
    assert not result.allowed
    assert set(result.reasons) >= {"DISABLED", "ACCOUNT_MISMATCH", "NOT_FLAT"}


def test_the_gate_passes_only_when_everything_is_right() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v2/account":
            return httpx.Response(200, json={"cash": "1000", "account_number": "PA123"})
        return httpx.Response(200, json=[])

    result = read_only_gate(
        _adapter(handler), enabled=True, expected_account="PA123", require_flat=True
    )
    assert result.allowed and result.reasons == ()


def test_trade_updates_record_each_fill_once_across_both_paths() -> None:
    stream = {
        "stream": "trade_updates",
        "data": {
            "event": "fill",
            "execution_id": "exec-1",
            "timestamp": "2026-10-29T14:00:01Z",
            "price": "100.04",
            "qty": "0.5",
            "order": {"id": "o1", "client_order_id": "c1", "symbol": "SPY", "side": "buy"},
        },
    }
    update = parse_trade_update(stream)
    assert update is not None
    seen: set[str] = set()
    assert len(new_fills([update, update], seen)) == 1
    assert new_fills([update], seen) == []  # the REST activity for the same fill adds nothing
    assert parse_trade_update({"stream": "authorization"}) is None


def test_capital_is_the_lower_of_strategy_and_broker_and_never_buying_power() -> None:
    strategy = strategy_equity(Decimal("1000.00"), Decimal("-40.00"))
    assert strategy == Decimal("960.00")
    assert risk_equity(strategy, Decimal("50000.00")) == Decimal("960.00")
    assert risk_equity(strategy, Decimal("300.00")) == Decimal("300.00")
    assert deployable_cash(Decimal("960"), Decimal("50000"), Decimal("200")) == Decimal("760")
    assert deployable_cash(Decimal("100"), Decimal("50000"), Decimal("500")) == Decimal(0)
