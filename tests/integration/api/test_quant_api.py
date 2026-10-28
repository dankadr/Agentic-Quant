from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0, make_account, make_instrument, make_snapshot
from tests.integration.api.conftest import bearer
from trading.api.auth import Role
from trading.core.config import load_config
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))
D = Decimal


@pytest.fixture(autouse=True)
def _strategies(db: Session) -> None:
    seed_strategies(db, CFG, now=T0)
    db.commit()


def _signal(
    db: Session, *, strategy: str = "orb_v1", minutes: int = 0, status: str = "ALLOCATED"
) -> None:
    inst = (
        make_instrument(db, "SPY")
        if not db.execute(text("select 1 from instruments")).first()
        else None
    )
    iid = inst.id if inst else db.execute(text("select id from instruments limit 1")).scalar_one()
    snap = make_snapshot(db, T0 + timedelta(minutes=minutes), data_hash=f"h{minutes}")
    db.execute(
        text(
            "insert into signals (id, strategy_id, strategy_version, instrument_id, generated_at, expires_at,"
            " snapshot_id, side, target_notional, stop_price, limit_price, reason_codes, status)"
            " values (gen_random_uuid(), :s, '1.0.0', :i, :g, :e, :snap, 'BUY', 80, 559, 561,"
            " ARRAY['ORB_BREAKOUT'], :st)"
        ),
        {
            "s": strategy,
            "i": iid,
            "g": T0 + timedelta(minutes=minutes),
            "e": T0 + timedelta(minutes=minutes + 1),
            "snap": snap.id,
            "st": status,
        },
    )
    db.commit()


# ---- reads ---------------------------------------------------------------------------------------


def test_strategies_are_listed_with_parameters_and_require_auth(
    client: TestClient, viewer_key: str
) -> None:
    assert client.get("/v1/strategies").status_code == 401
    body = client.get("/v1/strategies", headers=bearer(viewer_key)).json()
    orb = next(s for s in body["items"] if s["id"] == "orb_v1")
    assert orb["version"] == "1.0.0"
    assert orb["params"]["target_r"] == "1.5"
    assert orb["capital_budget"] == "350.0000"
    assert orb["enabled"] is True


def test_signals_are_paginated_newest_first_and_filterable(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    for m in range(3):
        _signal(db, minutes=m)
    _signal(db, strategy="vwap_v1", minutes=5, status="IGNORED")
    first = client.get("/v1/signals?limit=2", headers=bearer(viewer_key)).json()
    assert [s["strategy_id"] for s in first["items"]] == ["vwap_v1", "orb_v1"]
    assert first["items"][0]["symbol"] == "SPY"
    assert first["items"][1]["reason_codes"] == ["ORB_BREAKOUT"]
    assert first["items"][1]["target_notional"] == "80.0000"
    assert first["next_cursor"]
    rest = client.get(
        f"/v1/signals?limit=2&cursor={first['next_cursor']}", headers=bearer(viewer_key)
    ).json()
    assert len(rest["items"]) == 2
    assert rest["next_cursor"] is None
    only = client.get("/v1/signals?strategy_id=vwap_v1", headers=bearer(viewer_key)).json()
    assert [s["status"] for s in only["items"]] == ["IGNORED"]
    assert client.get("/v1/signals?status=BOGUS", headers=bearer(viewer_key)).status_code == 422


def test_performance_without_an_account_is_unavailable(client: TestClient, viewer_key: str) -> None:
    r = client.get("/v1/performance", headers=bearer(viewer_key))
    assert r.status_code == 503
    assert r.json()["code"] == "STATE_UNAVAILABLE"


def test_performance_reports_account_and_strategy_scope(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    acct = make_account(db)
    db.execute(
        text(
            "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure,"
            " realized_pnl, unrealized_pnl, positions_json) values (gen_random_uuid(), :a, :t, :k, 1000, 1000,"
            " :e, 0, 0, 0, '{}'::jsonb)"
        ),
        {"a": acct.id, "t": T0, "k": "SESSION_START", "e": D(1000)},
    )
    db.execute(
        text(
            "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure,"
            " realized_pnl, unrealized_pnl, positions_json) values (gen_random_uuid(), :a, :t, :k, 1010, 1010,"
            " :e, 0, 10, 0, '{}'::jsonb)"
        ),
        {"a": acct.id, "t": T0 + timedelta(hours=6), "k": "SESSION_END", "e": D(1010)},
    )
    db.commit()
    body = client.get(
        "/v1/performance?start=2026-10-19&end=2026-10-19", headers=bearer(viewer_key)
    ).json()
    assert body["scope"] == "account"
    assert body["net_pnl"] == "10.0000"
    assert body["return_pct"] is not None
    assert body["start_equity"] == "1000.0000"
    assert "equity_curve" not in body
    strat = client.get(
        "/v1/performance?start=2026-10-19&end=2026-10-19&strategy_id=orb_v1",
        headers=bearer(viewer_key),
    ).json()
    assert strat["scope"] == "orb_v1"
    assert strat["trades"]["trades"] == 0
    bad = client.get("/v1/performance?start=2026-10-20&end=2026-10-19", headers=bearer(viewer_key))
    assert bad.status_code == 422
    assert (
        client.get("/v1/performance?strategy_id=nope", headers=bearer(viewer_key)).status_code
        == 404
    )


# ---- config proposals ----------------------------------------------------------------------------


def _propose(client: TestClient, key: str, *, params: dict[str, object] | None = None, idem: str = "k1",
             base: str = "1.0.0", strategy: str = "orb_v1"):  # type: ignore[no-untyped-def]  # fmt: skip
    return client.post(
        f"/v1/strategies/{strategy}/propose-config",
        headers={**bearer(key), "Idempotency-Key": idem},
        json={
            "base_version": base,
            "parameters": params or {"target_r": "2.0"},
            "rationale": "wf supported",
        },
    )


def test_propose_creates_a_pending_proposal_and_never_touches_live_params(
    client: TestClient, db: Session, operator_key: str, viewer_key: str
) -> None:
    r = _propose(client, operator_key)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "PENDING_REVIEW"
    assert body["proposed_by"] == "op1"
    live = client.get("/v1/strategies", headers=bearer(viewer_key)).json()["items"]
    assert next(s for s in live if s["id"] == "orb_v1")["params"]["target_r"] == "1.5"
    listed = client.get(
        "/v1/config-proposals?status=PENDING_REVIEW", headers=bearer(viewer_key)
    ).json()
    assert [p["id"] for p in listed["items"]] == [body["id"]]


def test_propose_is_idempotent_and_requires_the_operator_role(
    client: TestClient, db: Session, operator_key: str, viewer_key: str
) -> None:
    a = _propose(client, operator_key, idem="same")
    b = _propose(client, operator_key, idem="same")
    assert b.headers["Idempotent-Replayed"] == "true"
    assert a.json() == b.json()
    assert db.execute(text("select count(*) from strategy_config_proposals")).scalar_one() == 1
    assert _propose(client, viewer_key, idem="v").status_code == 403
    no_key = client.post("/v1/strategies/orb_v1/propose-config", headers=bearer(operator_key),
                         json={"base_version": "1.0.0", "parameters": {"target_r": "2"}, "rationale": "r"})  # fmt: skip
    assert no_key.status_code == 400


@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"strategy": "nope"}, 404, "NOT_FOUND"),
        ({"base": "0.0.1"}, 409, "CONFLICT"),
        ({"params": {"target_r": "-3"}}, 422, "INVALID_PARAMETERS"),
        ({"params": {"target_r": 2.5}}, 422, "INVALID_PARAMETERS"),
        ({"params": {"enabled": False}}, 422, "INVALID_PARAMETERS"),
        ({"strategy": "eod_flatten", "params": {"flatten_minutes_before_close": 1}}, 422, "INVALID_PARAMETERS"),
    ],
)  # fmt: skip
def test_bad_proposals_are_rejected_with_problem_details(
    client: TestClient, operator_key: str, kwargs: dict[str, object], status: int, code: str
) -> None:
    r = _propose(client, operator_key, **kwargs)  # type: ignore[arg-type]
    assert r.status_code == status
    assert r.json()["code"] == code


def test_review_requires_a_different_principal_and_is_final(
    client: TestClient, db: Session, make_key: Callable[[str, Role], str], operator_key: str
) -> None:
    pid = _propose(client, operator_key).json()["id"]
    url = f"/v1/config-proposals/{pid}"
    own = client.post(
        f"{url}/approve", headers={**bearer(operator_key), "Idempotency-Key": "a"}, json={}
    )
    assert (own.status_code, own.json()["code"]) == (403, "FORBIDDEN")
    reviewer = make_key("reviewer", Role.OPERATOR)
    ok = client.post(
        f"{url}/approve", headers={**bearer(reviewer), "Idempotency-Key": "b"}, json={}
    )
    assert ok.status_code == 200
    assert (ok.json()["status"], ok.json()["reviewed_by"]) == ("APPROVED", "reviewer")
    again = client.post(
        f"{url}/reject", headers={**bearer(reviewer), "Idempotency-Key": "c"}, json={}
    )
    assert (again.status_code, again.json()["code"]) == (409, "CONFLICT")
    # approval alone never changes live parameters
    assert (
        db.execute(
            text("select params_json->>'target_r' from strategies where id='orb_v1'")
        ).scalar_one()
        == "1.5"
    )
    missing = client.post("/v1/config-proposals/00000000-0000-0000-0000-000000000001/approve",
                          headers={**bearer(reviewer), "Idempotency-Key": "d"}, json={})  # fmt: skip
    assert missing.status_code == 404


def test_a_viewer_cannot_review(client: TestClient, operator_key: str, viewer_key: str) -> None:
    pid = _propose(client, operator_key).json()["id"]
    r = client.post(
        f"/v1/config-proposals/{pid}/approve",
        headers={**bearer(viewer_key), "Idempotency-Key": "x"},
        json={},
    )
    assert r.status_code == 403


def test_performance_ignores_scratch_accounts(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    """Review I7: a replay/backtest account sharing the DB must not be reported as the portfolio."""
    scratch = make_account(db, name="replay-2026-10-19")
    for kind, equity, ts in (
        ("SESSION_START", 1000, T0),
        ("SESSION_END", 2000, T0 + timedelta(hours=6)),
    ):
        db.execute(
            text(
                "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure,"
                " realized_pnl, unrealized_pnl, positions_json) values (gen_random_uuid(), :a, :t, :k, :e, :e,"
                " :e, 0, 0, 0, '{}'::jsonb)"
            ),
            {"a": scratch.id, "t": ts, "k": kind, "e": D(equity)},
        )
    db.commit()
    r = client.get("/v1/performance", headers=bearer(viewer_key))
    assert r.status_code == 503  # no primary account has any snapshot yet
