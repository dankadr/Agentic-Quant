from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from sqlalchemy import select

from tests.factories import T0
from trading.agents.advisory import (
    run_data_quality_agent,
    run_execution_quality_agent,
    run_opportunities_agent,
)
from trading.core.config import load_config
from trading.data.instruments import upsert_universe
from trading.db.models.market import MarketBar, MarketQuote
from trading.db.models.research import AgentRun

CFG = load_config(Path("configs"))
NOW = T0 + timedelta(minutes=30, seconds=20)


def seed(db):
    instruments = upsert_universe(db, CFG.universe)
    for symbol in ("SPY", "QQQ"):
        inst = instruments[symbol]
        for minute in range(30):
            px = D("100") + D(minute) / 10
            db.add(
                MarketBar(
                    instrument_id=inst.id,
                    interval="1m",
                    ts=T0 + timedelta(minutes=minute),
                    provider="replay",
                    open=px,
                    high=px + 1,
                    low=px - 1,
                    close=px,
                    volume=D("2000") if minute >= 25 else D("1000"),
                    ingested_at=T0 + timedelta(minutes=minute + 1),
                    session_date=T0.date(),
                    source_seq=None,
                    quality_flags=[],
                )
            )
        db.add(
            MarketQuote(
                instrument_id=inst.id,
                provider="replay",
                ts=NOW - timedelta(seconds=5),
                bid=D("102.9"),
                ask=D("102.91"),
                bid_size=None,
                ask_size=None,
                ingested_at=NOW,
                quality_flags=[],
            )
        )
    db.commit()


def test_data_quality_uses_closed_ingested_bars_and_records_missing_symbols(
    db, session_factory, calendar
):
    seed(db)
    assert (
        run_data_quality_agent(
            session_factory, config=CFG, calendar=calendar, provider_name="replay", now=NOW
        )
        == "COMPLETED"
    )
    db.expire_all()
    run = db.scalar(select(AgentRun).where(AgentRun.agent_name == "data_quality"))
    assert run.result_json["coverage"]["SPY"]["bars"] == 30
    assert run.result_json["coverage"]["DIA"]["bars"] == 0
    assert "DIA: coverage below 95%" in run.result_json["evidence"]
    assert run.result_json["authority"] == "RESEARCH_ONLY"


def test_opportunities_ties_stable_and_excludes_missing_symbols(db, session_factory, calendar):
    seed(db)
    assert (
        run_opportunities_agent(
            session_factory, config=CFG, calendar=calendar, provider_name="replay", now=NOW
        )
        == "COMPLETED"
    )
    run = db.scalar(select(AgentRun).where(AgentRun.agent_name == "opportunities"))
    assert [r["symbol"] for r in run.result_json["items"]] == ["QQQ", "SPY"]
    assert run.result_json["items"][0]["relative_volume"] == "2"
    assert run.result_json["excluded"]["DIA"] == "incomplete_or_flagged_bars"


def test_opportunities_do_not_use_bars_ingested_after_observation(db, session_factory, calendar):
    seed(db)
    row = db.scalar(select(MarketBar).where(MarketBar.ts == T0).limit(1))
    row.ingested_at = NOW + timedelta(minutes=1)
    db.commit()
    run_opportunities_agent(
        session_factory, config=CFG, calendar=calendar, provider_name="replay", now=NOW
    )
    run = db.scalar(select(AgentRun).where(AgentRun.agent_name == "opportunities"))
    assert len(run.result_json["items"]) == 1


def test_missing_data_and_outside_session_are_visible_skips(db, session_factory, calendar):
    assert (
        run_opportunities_agent(
            session_factory, config=CFG, calendar=calendar, provider_name="replay", now=NOW
        )
        == "SKIPPED"
    )
    assert (
        run_data_quality_agent(
            session_factory,
            config=CFG,
            calendar=calendar,
            provider_name="replay",
            now=T0 - timedelta(hours=2),
        )
        == "SKIPPED"
    )
    results = list(db.scalars(select(AgentRun).order_by(AgentRun.started_at)))
    assert {r.result_json["skip_reason"] for r in results} == {
        "no_eligible_symbols",
        "outside_session",
    }


def test_execution_quality_skips_without_account_and_before_session_end(
    db, session_factory, calendar
):
    assert (
        run_execution_quality_agent(
            session_factory,
            config=CFG,
            calendar=calendar,
            provider_name="replay",
            session_date=T0.date(),
            now=NOW,
        )
        == "SKIPPED"
    )
    assert (
        run_execution_quality_agent(
            session_factory,
            config=CFG,
            calendar=calendar,
            provider_name="replay",
            session_date=T0.date(),
            now=T0 + timedelta(hours=8),
        )
        == "SKIPPED"
    )


def test_execution_quality_has_symbol_and_strategy_costs_without_double_counting(
    db, session_factory, calendar
):
    from tests.factories import full_chain
    from trading.db.models.portfolio import StrategyAttribution
    from trading.db.models.trading import FillRow
    from trading.quant.registry import seed_strategies

    seed_strategies(db, CFG, now=T0)
    _, _, _, _, order = full_chain(db)
    order.status = "FILLED"
    fill = FillRow(
        order_id=order.id,
        execution_id="review-fill",
        ts=T0,
        qty=D("1"),
        price=D("500"),
        fees=D("1.25"),
        slippage_estimate=D("2.50"),
    )
    db.add(fill)
    db.flush()
    db.add(
        StrategyAttribution(
            strategy_id="orb_v1",
            fill_id=fill.id,
            allocated_qty=fill.qty,
            allocated_fees=fill.fees,
            pnl=D("-1.25"),
        )
    )
    db.commit()
    assert (
        run_execution_quality_agent(
            session_factory,
            config=CFG,
            calendar=calendar,
            provider_name="replay",
            session_date=T0.date(),
            now=T0 + timedelta(hours=8),
        )
        == "COMPLETED"
    )
    result = db.scalar(
        select(AgentRun).where(AgentRun.agent_name == "execution_quality")
    ).result_json
    assert result["by_symbol"]["SPY"]["fees"] == "1.2500"
    assert result["by_strategy"]["orb_v1"]["fees"] == "1.2500"
    assert result["by_strategy"]["orb_v1"]["slippage_estimate"] == "2.5000"
    assert result["by_symbol"]["SPY"]["by_status"] == {"FILLED": 1}
    assert result["by_strategy"]["orb_v1"]["attributed_net_pnl"] == "-1.2500"
