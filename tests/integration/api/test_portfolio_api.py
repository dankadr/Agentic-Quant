from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.api.conftest import bearer
from tests.integration.execution.env import CFG, DAY, NOW, Env, build_env
from trading.core.clock import ManualClock
from trading.core.enums import Side
from trading.data.calendar import MarketCalendar
from trading.portfolio.valuation import record_equity_snapshot, value_portfolio

D = Decimal


@pytest.fixture
def env(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
) -> Env:
    return build_env(db, session_factory, clock, calendar)


def _snapshot(env: Env, mark: str = "100.00") -> None:
    with env.sf.begin() as s:
        state = value_portfolio(s, env.account_id, marks={"SPY": D(mark)}, ask_by_symbol={}, as_of=env.clock.now(),
                                session_date=DAY, cfg=CFG.paper_broker)  # fmt: skip
        record_equity_snapshot(s, state, kind="INTRADAY")


def _trade(env: Env, *, strategy: str = "orb_v1", qty: str = "0.5") -> None:
    order_id = env.place(qty=qty, strategy=strategy)
    env.manager.submit(order_id)
    env.tick()
    env.manager.sync_fills()


def test_portfolio_values_are_decimal_strings(
    env: Env, client: TestClient, viewer_key: str
) -> None:
    _trade(env)
    _snapshot(env, "101.00")
    body = client.get("/v1/portfolio", headers=bearer(viewer_key)).json()
    assert body["currency"] == "USD"
    assert body["as_of"].endswith("Z")
    assert body["cash"] == "949.9900"
    assert body["equity"] == "1000.4900"  # 949.99 + 0.5 * 101
    assert body["exposure"] == "50.5000"
    assert body["unrealized_pnl"] == "0.4900"
    assert body["realized_pnl"] == "0.0000"
    assert body["buying_power"] == "949.9900"
    assert all(isinstance(body[k], str) for k in ("cash", "equity", "buying_power", "exposure"))


def test_buying_power_subtracts_open_orders(env: Env, client: TestClient, viewer_key: str) -> None:
    _snapshot(env)
    env.manager.submit(env.place(qty="2", limit="99.00"))  # resting, reserves 198
    body = client.get("/v1/portfolio", headers=bearer(viewer_key)).json()
    assert body["buying_power"] == "802.0000"


def test_portfolio_503_before_any_snapshot(client: TestClient, viewer_key: str) -> None:
    r = client.get("/v1/portfolio", headers=bearer(viewer_key))
    assert r.status_code == 503
    assert r.json()["code"] == "STATE_UNAVAILABLE"


def test_read_endpoints_require_authentication(client: TestClient) -> None:
    for path in ("/v1/portfolio", "/v1/positions", "/v1/orders"):
        assert client.get(path).status_code == 401


def test_positions_with_attribution_and_filter(
    env: Env, client: TestClient, viewer_key: str
) -> None:
    _trade(env, strategy="orb_v1")
    _snapshot(env, "102.00")
    body = client.get("/v1/positions", headers=bearer(viewer_key)).json()
    (item,) = body["items"]
    assert item["symbol"] == "SPY"
    assert item["qty"] == "0.5"
    assert item["average_cost"] == "100.02"
    assert (item["mark"], item["market_value"], item["unrealized_pnl"]) == ("102", "51", "0.99")
    assert item["strategy_ids"] == ["orb_v1"]
    assert body["next_cursor"] is None
    hit = client.get(
        "/v1/positions", headers=bearer(viewer_key), params={"strategy_id": "orb_v1"}
    ).json()
    miss = client.get(
        "/v1/positions", headers=bearer(viewer_key), params={"strategy_id": "other"}
    ).json()
    assert len(hit["items"]) == 1
    assert miss["items"] == []


def test_flat_positions_are_not_listed(env: Env, client: TestClient, viewer_key: str) -> None:
    _trade(env)
    sell = env.place(side=Side.SELL, qty="0.5", limit="50.00")
    env.manager.submit(sell)
    env.tick()
    env.manager.sync_fills()
    assert client.get("/v1/positions", headers=bearer(viewer_key)).json()["items"] == []


def test_orders_include_fills_and_paginate(env: Env, client: TestClient, viewer_key: str) -> None:
    for _ in range(3):
        env.clock.advance(timedelta(seconds=1))
        _trade(env, qty="0.1")
    h = bearer(viewer_key)
    page1 = client.get("/v1/orders", headers=h, params={"limit": 2}).json()
    assert len(page1["items"]) == 2
    page2 = client.get(
        "/v1/orders", headers=h, params={"limit": 2, "cursor": page1["next_cursor"]}
    ).json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    ids = [i["client_order_id"] for i in page1["items"] + page2["items"]]
    assert len(set(ids)) == 3
    first = page1["items"][0]
    assert first["status"] == "FILLED"
    assert first["symbol"] == "SPY"
    assert first["qty"] == "0.1"
    assert first["filled_qty"] == "0.1"
    assert first["broker_order_id"].startswith("B-")
    assert [f["execution_id"] for f in first["fills"]] == [f"{first['client_order_id']}-1"]
    assert first["fills"][0]["price"] == "100.02"


def test_orders_status_filter_and_validation(env: Env, client: TestClient, viewer_key: str) -> None:
    _trade(env)
    resting = env.place(limit="99.00")
    env.manager.submit(resting)
    h = bearer(viewer_key)
    filled = client.get("/v1/orders", headers=h, params={"status": "FILLED"}).json()["items"]
    open_ = client.get("/v1/orders", headers=h, params={"status": "ACKNOWLEDGED"}).json()["items"]
    assert (len(filled), len(open_)) == (1, 1)
    bad = client.get("/v1/orders", headers=h, params={"status": "BOGUS"})
    assert bad.status_code == 422
    assert client.get("/v1/orders", headers=h, params={"cursor": "junk"}).status_code == 422


# ---- status enrichment / readiness -------------------------------------------------------------


def test_status_reports_session_and_last_data(
    env: Env, client: TestClient, viewer_key: str
) -> None:
    body = client.get("/v1/system/status", headers=bearer(viewer_key)).json()
    assert body["session"] == {
        "date": "2026-10-19", "open_at": "2026-10-19T13:30:00Z", "close_at": "2026-10-19T20:00:00Z",
        "is_open": True, "is_half_day": False,
    }  # fmt: skip
    assert body["last_data_at"] is None
    with env.sf.begin() as s:
        s.execute(text("insert into instruments(symbol, venue, asset_type, tick_size, lot_size, fractional_eligible, sector)"
                       " values ('ZZZ','XNYS','ETF',0.01,1,true,'x') on conflict do nothing"))  # fmt: skip
        iid = s.execute(text("select id from instruments where symbol='ZZZ'")).scalar_one()
        s.execute(text("insert into market_bars(instrument_id, interval, ts, open, high, low, close, volume, provider, ingested_at)"
                       " values (:i,'1m',:t,1,1,1,1,1,'p',:t)"), {"i": iid, "t": NOW})  # fmt: skip
    assert (
        client.get("/v1/system/status", headers=bearer(viewer_key)).json()["last_data_at"]
        == "2026-10-19T15:00:00Z"
    )


def test_status_session_is_null_when_closed(
    env: Env, client: TestClient, viewer_key: str, clock: ManualClock
) -> None:
    clock.set(NOW.replace(day=24))  # a Saturday
    assert client.get("/v1/system/status", headers=bearer(viewer_key)).json()["session"] is None


def test_market_data_readiness_states(
    env: Env, clock: ManualClock, calendar: MarketCalendar
) -> None:
    from trading.api.routes.health import compute_readiness

    def state() -> str:
        return compute_readiness(env.sf, clock, calendar).market_data

    def bar_for(s, iid) -> None:  # type: ignore[no-untyped-def]
        # A bar that opened 90 s ago (its ts is the bar START), so it closed 30 s ago: fresh.
        s.execute(text("insert into market_bars(instrument_id, interval, ts, open, high, low, close, volume, provider, ingested_at)"
                       " values (:i,'1m',:t,1,1,1,1,1,'p',:t)"), {"i": iid, "t": NOW - timedelta(seconds=90)})  # fmt: skip

    assert state() == "not_configured"  # no bars have ever been ingested
    with env.sf.begin() as s:
        ids = [r[0] for r in s.execute(text("select id from instruments order by symbol")).all()]
        for iid in ids[:-1]:
            bar_for(s, iid)
    assert state() == "incomplete"  # one configured instrument has no bars at all
    with env.sf.begin() as s:
        bar_for(s, ids[-1])
    assert state() == "ok"
    clock.advance(timedelta(seconds=200))
    assert state() == "stale"
    clock.set(NOW.replace(hour=22))
    assert state() == "closed"
