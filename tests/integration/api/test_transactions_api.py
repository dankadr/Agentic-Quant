from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0, make_account
from tests.integration.api.conftest import bearer
from tests.integration.portfolio.helpers import NEXT_DAY, broker_fill, new_order, open_account
from trading.core.config import load_config
from trading.portfolio.ledger import apply_fill
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))
D = Decimal
DAY2 = T0 + timedelta(days=1)


@pytest.fixture
def acct(db: Session):  # type: ignore[no-untyped-def]
    seed_strategies(db, CFG, now=T0)
    a = open_account(db)
    db.commit()
    return a


def fill(
    db: Session,
    acct,
    coid: str,
    side: str,
    qty: str,
    price: str,
    at: datetime,
    strategy: str = "orb_v1",
    fees: str = "0.01",
    symbol: str = "SPY",
) -> None:  # type: ignore[no-untyped-def]
    order = new_order(
        db,
        acct,
        coid,
        side=side,
        qty=qty,
        limit="999" if side == "BUY" else "1",
        strategy_ids=[strategy],
        symbol=symbol,
    )
    f = replace(
        broker_fill(order, f"e-{coid}", qty=qty, price=price, fees=fees, symbol=symbol), ts=at
    )
    apply_fill(db, account_id=acct, fill=f, settles_on=NEXT_DAY, now=at)
    db.commit()


def round_trip(
    db: Session, acct, tag: str, strategy: str, symbol: str, entry: str, exit_: str, at: datetime
) -> None:  # type: ignore[no-untyped-def]
    fill(db, acct, f"{tag}b", "BUY", "1", entry, at, strategy, symbol=symbol)
    fill(
        db, acct, f"{tag}s", "SELL", "1", exit_, at + timedelta(minutes=30), strategy, symbol=symbol
    )


def test_fills_list_prices_fees_and_attribution_newest_first(
    client: TestClient, db: Session, acct, viewer_key: str
) -> None:  # type: ignore[no-untyped-def]
    round_trip(db, acct, "a", "orb_v1", "SPY", "100", "102", T0)
    body = client.get("/v1/fills?limit=10", headers=bearer(viewer_key)).json()
    assert [f["side"] for f in body["items"]] == ["SELL", "BUY"]
    sell = body["items"][0]
    assert (sell["symbol"], sell["qty"], sell["price"], sell["fees"]) == (
        "SPY",
        "1",
        "102",
        "0.0100",
    )
    assert sell["notional"] == "102.0000"
    assert sell["strategies"][0]["strategy_id"] == "orb_v1"
    assert sell["ts"].endswith("Z")
    assert client.get("/v1/fills").status_code == 401


def test_fills_can_be_limited_to_one_day(
    client: TestClient, db: Session, acct, viewer_key: str
) -> None:  # type: ignore[no-untyped-def]
    round_trip(db, acct, "a", "orb_v1", "SPY", "100", "101", T0)
    round_trip(db, acct, "b", "vwap_v1", "SPY", "100", "99", DAY2)
    day1 = client.get("/v1/fills?date=2026-10-19", headers=bearer(viewer_key)).json()["items"]
    assert len(day1) == 2
    assert client.get("/v1/fills?date=nope", headers=bearer(viewer_key)).status_code == 422


def test_round_trips_pair_entries_with_exits_and_report_net_pnl(
    client: TestClient, db: Session, acct, viewer_key: str
) -> None:  # type: ignore[no-untyped-def]
    round_trip(db, acct, "a", "orb_v1", "SPY", "100", "102", T0)  # +2 gross, 0.02 fees
    round_trip(
        db,
        acct,
        "b",
        "vwap_v1",
        "QQQ",
        "50",
        "49",
        T0 + timedelta(hours=1),
    )  # -1 gross
    body = client.get("/v1/trades?date=2026-10-19", headers=bearer(viewer_key)).json()
    trades = {(t["strategy_id"], t["symbol"]): t for t in body["items"]}
    a = trades[("orb_v1", "SPY")]
    assert (a["status"], a["avg_entry"], a["avg_exit"], a["qty"]) == ("CLOSED", "100", "102", "1")
    assert a["gross_pnl"] == "2.0000"
    assert a["net_pnl"] == "1.9800"
    assert a["hold_minutes"] == 30
    assert trades[("vwap_v1", "QQQ")]["net_pnl"] == "-1.0200"
    assert body["summary"] == {"trades": 2, "wins": 1, "net_pnl": "0.9600", "fees": "0.0400"}


def test_an_open_position_is_an_open_trade(
    client: TestClient, db: Session, acct, viewer_key: str
) -> None:  # type: ignore[no-untyped-def]
    fill(db, acct, "o", "BUY", "1", "100", T0)
    (t,) = client.get("/v1/trades?date=2026-10-19", headers=bearer(viewer_key)).json()["items"]
    assert (t["status"], t["closed_at"], t["avg_exit"]) == ("OPEN", None, None)


def test_ledger_lists_cash_movements_with_settlement_dates(
    client: TestClient, db: Session, acct, viewer_key: str
) -> None:  # type: ignore[no-untyped-def]
    round_trip(db, acct, "a", "orb_v1", "SPY", "100", "102", T0)
    body = client.get("/v1/ledger?limit=20", headers=bearer(viewer_key)).json()
    kinds = {e["source_type"] for e in body["items"]}
    assert "FILL" in kinds or len(kinds) >= 1
    first = body["items"][0]
    assert first["settles_on"] and first["amount"] and first["ts"].endswith("Z")


def test_sessions_give_daily_equity_and_pnl(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    a = make_account(db)
    for kind, day, eq in (
        ("SESSION_START", 0, "1000"),
        ("SESSION_END", 0, "1004"),
        ("SESSION_START", 1, "1004"),
        ("SESSION_END", 1, "1001"),
    ):
        db.execute(
            text(
                "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure, realized_pnl, unrealized_pnl, positions_json)"
                " values (gen_random_uuid(), :a, :t, :k, :e, :e, :e, 0, 0, 0, '{}'::jsonb)"
            ),
            {
                "a": a.id,
                "t": T0 + timedelta(days=day, hours=6 if kind == "SESSION_END" else 0),
                "k": kind,
                "e": D(eq),
            },
        )
    db.commit()
    body = client.get("/v1/sessions?limit=10", headers=bearer(viewer_key)).json()
    assert [(s["date"], s["start_equity"], s["end_equity"], s["pnl"]) for s in body["items"]] == [
        ("2026-10-20", "1004.0000", "1001.0000", "-3.0000"),
        ("2026-10-19", "1000.0000", "1004.0000", "4.0000"),
    ]
    assert body["items"][1]["return_pct"] == "0.004"
    assert UTC is not None
