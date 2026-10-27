import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.factories import T0, make_account
from tests.integration.api.conftest import bearer

D = Decimal
STATIC = Path("src/trading/api/static")


def agent_run(
    db: Session, name: str, status: str, minutes: int, result: dict[str, object] | None = None
) -> None:
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at, completed_at, result_json, status)"
            " values (gen_random_uuid(), :n, 'm', 'v1', :t, :t, CAST(:r AS jsonb), :s)"
        ),
        {
            "n": name,
            "t": T0 + timedelta(minutes=minutes),
            "r": json.dumps(result or {}),
            "s": status,
        },
    )
    db.commit()


def snapshot(db: Session, acct, kind: str, minutes: int, equity: str) -> None:  # type: ignore[no-untyped-def]
    db.execute(
        text(
            "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure, realized_pnl,"
            " unrealized_pnl, positions_json) values (gen_random_uuid(), :a, :t, :k, :e, :e, :e, 0, 0, 0, '{}'::jsonb)"
        ),
        {"a": acct.id, "t": T0 + timedelta(minutes=minutes), "k": kind, "e": D(equity)},
    )
    db.commit()


# ---- agent runs ----------------------------------------------------------------------------------


def test_agent_runs_are_listed_newest_first_and_filterable(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    agent_run(db, "macro", "COMPLETED", 1, {"regime": "RANGING", "risk_multiplier": "0.7"})
    agent_run(db, "evaluator", "COMPLETED", 2, {"summary": "quiet day"})
    agent_run(db, "macro", "FAILED", 3, {"error": "insufficient_data"})
    body = client.get("/v1/agents/runs", headers=bearer(viewer_key)).json()
    assert [(r["agent_name"], r["status"]) for r in body["items"]] == [
        ("macro", "FAILED"),
        ("evaluator", "COMPLETED"),
        ("macro", "COMPLETED"),
    ]
    assert body["items"][2]["result"]["regime"] == "RANGING"
    only = client.get("/v1/agents/runs?agent_name=macro&limit=1", headers=bearer(viewer_key)).json()
    assert [r["status"] for r in only["items"]] == ["FAILED"]
    assert client.get("/v1/agents/runs").status_code == 401


# ---- equity curve --------------------------------------------------------------------------------


def test_equity_curve_returns_the_primary_accounts_intraday_points(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    acct = make_account(db)
    scratch = make_account(db, name="replay-2026-10-19")
    snapshot(db, acct, "SESSION_START", 0, "1000")
    snapshot(db, acct, "INTRADAY", 1, "1001")
    snapshot(db, acct, "INTRADAY", 2, "999.5")
    snapshot(db, scratch, "INTRADAY", 3, "5000")
    body = client.get("/v1/equity?date=2026-10-19", headers=bearer(viewer_key)).json()
    assert [p["equity"] for p in body["points"]] == ["1000.0000", "1001.0000", "999.5000"]
    assert body["points"][0]["ts"].endswith("Z")
    assert client.get("/v1/equity?date=nope", headers=bearer(viewer_key)).status_code == 422


def test_equity_curve_is_empty_but_ok_without_data(client: TestClient, viewer_key: str) -> None:
    body = client.get("/v1/equity?date=2026-10-19", headers=bearer(viewer_key)).json()
    assert body["points"] == []


# ---- daily report --------------------------------------------------------------------------------


def test_daily_report_endpoint_wraps_the_report(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    acct = make_account(db)
    snapshot(db, acct, "SESSION_START", 0, "1000")
    snapshot(db, acct, "SESSION_END", 390, "1003")
    r = client.get("/v1/report/daily?date=2026-10-19", headers=bearer(viewer_key))
    assert r.status_code == 200
    body = r.json()
    assert body["date"] == "2026-10-19"
    assert body["equity"]["pnl"] == "3.0000"
    assert "data_coverage" in body and "strategies" in body
    assert client.get("/v1/report/daily?date=bogus", headers=bearer(viewer_key)).status_code == 422


def test_daily_report_without_an_account_is_unavailable(
    client: TestClient, viewer_key: str
) -> None:
    assert (
        client.get("/v1/report/daily?date=2026-10-19", headers=bearer(viewer_key)).status_code
        == 503
    )


# ---- static dashboard ----------------------------------------------------------------------------


def test_dashboard_page_is_served_with_a_strict_csp(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/dashboard/app.js").status_code == 200
    assert client.get("/dashboard/style.css").status_code == 200
    assert client.get("/dashboard/../../etc/passwd").status_code == 404


def test_dashboard_code_never_renders_untrusted_text_as_html() -> None:
    """Agent output and news headlines are untrusted: only textContent may ever be used."""
    files = {
        f.name: f.read_text() for f in STATIC.iterdir() if f.suffix in (".js", ".html", ".css")
    }
    assert {
        "index.html",
        "app.js",
        "views.js",
        "charts.js",
        "util.js",
        "api.js",
        "agentpanel.js",
        "style.css",
    } <= set(files)
    for name, body in files.items():
        if name.endswith(".js"):
            for banned in (
                "innerHTML",
                "outerHTML",
                "insertAdjacentHTML",
                "document.write",
                "eval(",
                "new Function",
                'setAttribute("style"',
            ):
                assert banned not in body, (name, banned)
    html = files["index.html"]
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)  # no inline scripts
    assert not re.search(r"\son\w+\s*=", html)  # no inline event handlers
    assert 'style="' not in html  # the CSP forbids inline style attributes
    # no external calls or CDNs; the SVG namespace is an identifier, not a request
    blob = "\n".join(files.values()).replace("http://www.w3.org/2000/svg", "")
    assert re.findall(r"https?://[^\s\"')]+", blob) == []


def test_agents_view_is_wired_to_the_agent_panel_endpoints() -> None:
    views = (STATIC / "views.js").read_text()
    panel = (STATIC / "agentpanel.js").read_text()
    assert 'from "./agentpanel.js"' in views
    for path in ("/v1/agents", "/decisions", "/suggestions"):
        assert path in panel, path
    app_js = (STATIC / "app.js").read_text()
    for path in ("/config-proposals", "/v1/agent-config-proposals/"):
        assert path in app_js, path
    # locked scopes are shown, never offered as inputs
    assert "locked" in panel
    # enabling an agent warns about what it needs (the API cannot see the worker's secrets)
    assert "requires" in panel and "requires" in app_js


# ---- data coverage and research ------------------------------------------------------------------


def bar(db: Session, symbol: str, minute: int) -> None:
    from tests.factories import make_instrument

    inst = (
        db.execute(text("select id from instruments where symbol = :s"), {"s": symbol}).scalar()
        or make_instrument(db, symbol).id
    )
    from datetime import UTC, datetime

    ts = datetime(2026, 10, 19, 13, 30, tzinfo=UTC) + timedelta(minutes=minute)
    db.execute(
        text(
            "insert into market_bars (instrument_id, interval, ts, provider, open, high, low, close, volume, ingested_at,"
            " session_date, source_seq, quality_flags) values (:i, '1m', :t, 'alpaca-iex', 1, 1, 1, 1, 10, :t, '2026-10-19', null, :f)"
        ),
        {"i": inst, "t": ts, "f": ["GAP_BEFORE"] if minute == 31 else []},
    )


def test_data_coverage_buckets_bars_by_half_hour_per_symbol(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    for m in range(0, 30):  # a full first half hour for SPY
        bar(db, "SPY", m)
    for m in (30, 31, 32):  # three bars in the second half hour (one flagged)
        bar(db, "SPY", m)
    db.commit()
    body = client.get("/v1/data/coverage?date=2026-10-19", headers=bearer(viewer_key)).json()
    assert body["bucket_minutes"] == 30
    assert body["buckets"] == 13
    spy = next(s for s in body["symbols"] if s["symbol"] == "SPY")
    assert spy["bars"][:2] == [30, 3]
    assert spy["flagged"][:2] == [0, 1]
    assert spy["bars"][2:] == [0] * 11
    assert (
        client.get("/v1/data/coverage?date=2026-10-17", headers=bearer(viewer_key)).json()[
            "symbols"
        ]
        == []
    )  # a Saturday
    assert client.get("/v1/data/coverage?date=nope", headers=bearer(viewer_key)).status_code == 422


def test_research_experiments_are_listed_newest_first(
    client: TestClient, db: Session, viewer_key: str
) -> None:
    from decimal import Decimal as Dec

    from trading.core.config import load_config
    from trading.quant.registry import seed_strategies
    from trading.quant.research import record_experiment

    seed_strategies(db, load_config(Path("configs")), now=T0)

    record_experiment(db, kind="WALK_FORWARD", strategy_id="orb_v1", hypothesis="older", params={"a": 1}, data_range={"days": 4},
                      metrics={"verdict": "REJECTED", "p": Dec("0.5")}, status="REJECTED", now=T0)  # fmt: skip
    record_experiment(db, kind="SENSITIVITY", strategy_id="orb_v1", hypothesis="newer", params={}, data_range={},
                      metrics={"robust": False}, status="REJECTED", now=T0 + timedelta(minutes=1))  # fmt: skip
    db.commit()
    body = client.get("/v1/research/experiments?limit=10", headers=bearer(viewer_key)).json()
    assert [e["hypothesis"] for e in body["items"]] == ["newer", "older"]
    assert body["items"][1]["metrics"]["verdict"] == "REJECTED"
    assert body["items"][1]["created_at"].endswith("Z")
    assert client.get("/v1/research/experiments").status_code == 401


def test_static_assets_are_whitelisted_by_name(client: TestClient) -> None:
    assert client.get("/dashboard/app.js").status_code == 200
    for bad in ("../app.js", "..%2F..%2Fetc%2Fpasswd", "nope.js", ".env", "app.js/../x"):
        assert client.get(f"/dashboard/{bad}").status_code == 404
