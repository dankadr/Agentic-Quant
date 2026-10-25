"""A real server (in a thread) with seeded data, and a headless Chromium to drive the dashboard."""

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import make_account
from trading.api.app import create_app
from trading.api.auth import Role, create_principal
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.data.calendar import MarketCalendar
from trading.data.instruments import upsert_universe
from trading.quant.proposals import propose_config
from trading.quant.registry import seed_strategies
from trading.quant.research import record_experiment

CFG = load_config(Path("configs"))
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
NOW = OPEN + timedelta(minutes=90)  # 15:00Z, mid-session
XSS = "<img src=x onerror=window.__pwned=1><script>window.__pwned=2</script>"


@dataclass
class Demo:
    url: str
    viewer_key: str
    operator_key: str
    reviewer_key: str


def _seed(db: Session, sf: sessionmaker[Session]) -> dict[str, str]:
    instruments = upsert_universe(db, CFG.universe)
    db.execute(
        text("update system_state set trading_state = 'PAPER_RUNNING', risk_state = 'NORMAL'")
    )
    seed_strategies(db, CFG, now=OPEN)
    acct = make_account(db)
    for i in range(0, 61):
        kind = "SESSION_START" if i == 0 else "INTRADAY"
        eq = Decimal("1000") + Decimal(str(round((i % 7 - 3) * 0.15 + i * 0.02, 2)))
        db.execute(
            text(
                "insert into equity_snapshots (id, account_id, ts, kind, cash, settled_cash, equity, exposure, realized_pnl,"
                " unrealized_pnl, positions_json) values (gen_random_uuid(), :a, :t, :k, :e, :e, :e, 0, 0, 0, '{}'::jsonb)"
            ),
            {"a": acct.id, "t": OPEN + timedelta(minutes=i * 1.5), "k": kind, "e": eq},
        )
    spy = instruments["SPY"].id
    for m in range(0, 60):
        db.execute(
            text(
                "insert into market_bars (instrument_id, interval, ts, provider, open, high, low, close, volume, ingested_at,"
                " session_date, source_seq, quality_flags) values (:i, '1m', :t, 'alpaca-iex', 1, 1, 1, 1, 10, :t, :d, null, '{}')"
            ),
            {"i": spy, "t": OPEN + timedelta(minutes=m), "d": OPEN.date()},
        )

    def run(name: str, status: str, minutes: int, result: dict[str, object]) -> None:
        db.execute(
            text(
                "insert into agent_runs (id, agent_name, model, prompt_version, started_at, completed_at, result_json, status)"
                " values (gen_random_uuid(), :n, 'qwen-test', 'v1', :t, :t, CAST(:r AS jsonb), :s)"
            ),
            {
                "n": name,
                "t": OPEN + timedelta(minutes=minutes),
                "r": json.dumps(result),
                "s": status,
            },
        )

    run("macro", "COMPLETED", 20, {"regime": "RANGING", "risk_multiplier": "0.7", "confidence": 0.6, "conclusion": "RANGING: x0.7",
        "valid_until": (OPEN + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"), "evidence": [XSS, "efficiency is low"], "limitations": [XSS]})  # fmt: skip
    run("fundamental", "COMPLETED", 10, {"watchlist": [{"symbol": "AAPL", "rank": 1, "reason": XSS, "confidence": 0.8}],
        "catalysts": [{"symbol": "AAPL", "kind": "EARNINGS", "description": XSS, "event_date": None, "source_ids": ["1"]}], "limitations": [XSS]})  # fmt: skip
    run(
        "evaluator",
        "COMPLETED",
        30,
        {"summary": XSS + " quiet day", "findings": [XSS], "suggestions": []},
    )
    run("technical", "FAILED", 40, {"error": "no_experiments " + XSS})
    record_experiment(db, kind="WALK_FORWARD", strategy_id="orb_v1", hypothesis=XSS, params={"space": {"target_r": ["1.0", "2.0"]}},
                      data_range={"days": 20}, metrics={"verdict": "SUPPORTED", "out_of_sample_net_pnl": Decimal("5.5"), "p_value": Decimal("0.02")},
                      status="COMPLETED", now=OPEN)  # fmt: skip
    propose_config(db, strategy_id="orb_v1", base_version="1.0.0", parameters={"target_r": "2.0"}, rationale="walk-forward supports it " + XSS,
                   proposed_by="agent:technical", universe={u.symbol for u in CFG.universe}, now=OPEN)  # fmt: skip
    db.commit()
    keys = {}
    for name, role in (
        ("viewer1", Role.VIEWER),
        ("op1", Role.OPERATOR),
        ("reviewer", Role.OPERATOR),
    ):
        with sf.begin() as s:
            _, keys[name] = create_principal(s, name=name, role=role, now=OPEN)
    return keys


@pytest.fixture
def demo(
    db: Session, session_factory: sessionmaker[Session], calendar: MarketCalendar
) -> Iterator[Demo]:
    keys = _seed(db, session_factory)
    app = create_app(
        settings=Settings(),
        config=CFG,
        session_factory=session_factory,
        clock=ManualClock(NOW),
        calendar=calendar,
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "server did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield Demo(f"http://127.0.0.1:{port}", keys["viewer1"], keys["op1"], keys["reviewer"])
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def browser() -> Iterator[object]:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def new_page(browser) -> Iterator[Callable[..., object]]:  # type: ignore[no-untyped-def]
    contexts = []

    def make(**kwargs):  # type: ignore[no-untyped-def]
        ctx = browser.new_context(
            viewport=kwargs.pop("viewport", {"width": 1280, "height": 900}), **kwargs
        )
        contexts.append(ctx)
        page = ctx.new_page()
        page.problems = []  # type: ignore[attr-defined]
        page.requests = []  # type: ignore[attr-defined]
        page.on(
            "console",
            lambda m: (
                page.problems.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning")
                else None
            ),
        )  # type: ignore[attr-defined]
        page.on("pageerror", lambda e: page.problems.append(f"pageerror: {e}"))  # type: ignore[attr-defined]
        page.on("request", lambda r: page.requests.append(r.url))  # type: ignore[attr-defined]
        return page

    yield make
    for c in contexts:
        c.close()
