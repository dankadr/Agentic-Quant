import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0, make_snapshot
from tests.integration.portfolio.helpers import (
    NEXT_DAY,
    broker_fill,
    new_order,
    open_account,
)
from trading.agents.consensus import run_consensus_agent
from trading.core.config import load_config
from trading.portfolio.ledger import apply_fill
from trading.quant.registry import seed_strategies

CFG = load_config(Path("configs"))
D = Decimal
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
DAY = OPEN.date()
END = OPEN + timedelta(hours=7)


def macro(db: Session, regime: str, multiplier: str, at: datetime) -> None:
    result = {
        "regime": regime,
        "risk_multiplier": multiplier,
        "valid_until": (at + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    db.execute(
        text(
            "insert into agent_runs (id, agent_name, model, prompt_version, started_at, completed_at, result_json, status)"
            " values (gen_random_uuid(), 'macro', 'm', 'macro-v1', :t, :t, CAST(:r AS jsonb), 'COMPLETED')"
        ),
        {"t": at, "r": json.dumps(result)},
    )


def trade(
    db: Session, acct, coid: str, strategy: str, symbol: str, entry: str, exit_: str, at: datetime
) -> None:  # type: ignore[no-untyped-def]
    buy = new_order(
        db, acct, f"{coid}b", qty="1", limit="999", strategy_ids=[strategy], symbol=symbol
    )
    apply_fill(db, account_id=acct, fill=replace(broker_fill(buy, f"{coid}be", qty="1", price=entry, symbol=symbol), ts=at), settles_on=NEXT_DAY, now=at)  # fmt: skip
    sell = new_order(
        db,
        acct,
        f"{coid}s",
        side="SELL",
        qty="1",
        limit="1",
        strategy_ids=[strategy],
        symbol=symbol,
    )
    later = at + timedelta(minutes=30)
    apply_fill(db, account_id=acct, fill=replace(broker_fill(sell, f"{coid}se", qty="1", price=exit_, symbol=symbol), ts=later), settles_on=NEXT_DAY, now=later)  # fmt: skip


def signal(db: Session, strategy: str, symbol: str, side: str, at: datetime) -> None:
    inst = get_instrument(db, symbol).id
    snap = make_snapshot(db, at, data_hash=f"{strategy}{symbol}{side}{at}")
    db.execute(
        text(
            "insert into signals (id, strategy_id, strategy_version, instrument_id, generated_at, expires_at, snapshot_id,"
            " side, target_notional, reason_codes, status) values (gen_random_uuid(), :s, '1.0.0', :i, :g, :e, :snap, :side,"
            " 80, ARRAY['X'], 'ALLOCATED')"
        ),
        {
            "s": strategy,
            "i": inst,
            "g": at,
            "e": at + timedelta(minutes=1),
            "snap": snap.id,
            "side": side,
        },
    )


def test_consensus_scores_agreement_and_the_counterfactual(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seed_strategies(db, CFG, now=T0)
    acct = open_account(db)
    macro(db, "TRENDING", "1.0", OPEN + timedelta(minutes=16))
    # two entries under a healthy view: one wins (+2), one loses (-1)
    trade(db, acct, "a", "orb_v1", "SPY", "100", "102", OPEN + timedelta(minutes=20))
    trade(db, acct, "b", "vwap_v1", "SPY", "100", "99", OPEN + timedelta(minutes=25))
    # a view that turns defensive: this entry would have been vetoed by consensus (and it lost -3)
    macro(db, "HIGH_VOLATILITY", "0.2", OPEN + timedelta(hours=3))
    trade(db, acct, "c", "orb_v1", "QQQ", "100", "97", OPEN + timedelta(hours=3, minutes=5))
    db.commit()
    assert (
        run_consensus_agent(session_factory, config=CFG, session_date=DAY, now=END) == "COMPLETED"
    )
    row = db.execute(
        text("select model, result_json from agent_runs where agent_name = 'consensus'")
    ).one()
    r = row.result_json
    assert row.model == "deterministic"
    assert r["entries"] == 3
    assert (r["agreed"], r["vetoed"]) == (2, 1)
    assert D(str(r["base_pnl"])) == D("-2")  # +2 -1 -3
    assert D(str(r["consensus_pnl"])) == D("1")  # only the two agreed entries
    assert D(str(r["missed_pnl"])) == D("-3")  # what consensus would have skipped
    assert D(str(r["value_added"])) == D("3")  # skipping a loser helped
    assert r["verdict"] == "SHADOW_ONLY"  # one day cannot prove anything


def test_an_opposing_signal_from_another_strategy_is_a_conflict(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seed_strategies(db, CFG, now=T0)
    acct = open_account(db)
    macro(db, "TRENDING", "1.0", OPEN + timedelta(minutes=16))
    at = OPEN + timedelta(minutes=20)
    signal(db, "orb_v1", "SPY", "BUY", at)
    signal(db, "vwap_v1", "SPY", "SELL", at + timedelta(seconds=20))
    trade(db, acct, "a", "orb_v1", "SPY", "100", "101", at + timedelta(seconds=5))
    db.commit()
    run_consensus_agent(session_factory, config=CFG, session_date=DAY, now=END)
    r = db.execute(
        text("select result_json from agent_runs where agent_name = 'consensus'")
    ).scalar_one()
    assert (r["agreed"], r["vetoed"], r["conflicts"]) == (0, 1, 1)


def test_a_day_without_trades_reports_zero_and_never_fails(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    seed_strategies(db, CFG, now=T0)
    open_account(db)
    db.commit()
    assert (
        run_consensus_agent(session_factory, config=CFG, session_date=DAY, now=END) == "COMPLETED"
    )
    r = db.execute(
        text("select result_json from agent_runs where agent_name = 'consensus'")
    ).scalar_one()
    assert r["entries"] == 0
    assert D(str(r["value_added"])) == 0
