from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.integration.quant.test_backtest import _fixtures
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.db.models.research import AgentRun
from trading.quant.backtest import run_backtest
from trading.services.heavy import build_heavy_handlers, scratch_lock


def test_heavy_scratch_lock_serializes_workers(session_factory, db):
    with scratch_lock(session_factory):
        with pytest.raises(ValueError, match="scratch_busy"), scratch_lock(session_factory):
            pass
    with scratch_lock(session_factory):
        pass


def test_backtest_prepares_causal_macro_history_after_reset(session_factory, db, tmp_path):
    day = date(2026, 10, 19)
    fixtures = _fixtures(tmp_path, [day], ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA"])
    at = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)

    def prepare(sf):
        with sf.begin() as session:
            session.add(
                AgentRun(
                    agent_name="macro",
                    model="test",
                    prompt_version="test",
                    started_at=at,
                    completed_at=at,
                    available_at=at,
                    status="COMPLETED",
                    result_json={"marker": "history"},
                )
            )

    run_backtest(
        session_factory,
        fixtures_dir=fixtures,
        start=day,
        end=day,
        config=load_config(Path("configs")),
        minutes_per_day=2,
        prepare=prepare,
        tick_times={at.replace(minute=31): at.replace(minute=31, second=5, microsecond=100000)},
    )
    with session_factory() as session:
        ticks = (
            session.execute(text("SELECT ts FROM equity_snapshots WHERE kind='INTRADAY'"))
            .scalars()
            .all()
        )
        assert ticks == [at.replace(minute=31, second=5, microsecond=100000)]
        assert (
            session.execute(
                text("SELECT count(*) FROM agent_runs WHERE result_json->>'marker'='history'")
            ).scalar_one()
            == 1
        )


def test_missing_scratch_is_visible_not_silent(session_factory, db, calendar, tmp_path):
    from types import SimpleNamespace

    clock = ManualClock(datetime(2026, 11, 14, 8, 0, tzinfo=UTC))
    handlers = build_heavy_handlers(
        sf=session_factory,
        settings=Settings(),
        config=load_config(Path("configs")),
        calendar=calendar,
        clock=clock,
        provider=None,
        provider_name="replay",
        llm=None,
        root=tmp_path,
    )
    handlers["quant_research"](SimpleNamespace(kind="quant_research", payload={}))
    with session_factory() as session:
        row = session.execute(
            text("SELECT status,result_json FROM agent_runs WHERE agent_name='technical'")
        ).one()
        assert row.status == "SKIPPED"
        assert row.result_json["skip_reason"] == "scratch_not_configured"


def test_comparison_queue_captures_configuration_at_session_boundary(session_factory, db, calendar):
    from trading.services.scheduler import tick

    config = load_config(Path("configs"))
    clock = ManualClock(datetime(2026, 10, 19, 20, 15, 30, tzinfo=UTC))
    tick(session_factory, clock, calendar, config.agents, config=config)
    with session_factory() as session:
        payload = session.execute(
            text("SELECT payload_json FROM jobs WHERE kind='paper_comparison'")
        ).scalar_one()
        assert payload["session_config"]["risk"]["max_new_entries_per_day"] == 10
        assert (
            payload["session_config"]["strategies"]["orb_v1"]["params"][
                "max_entries_per_symbol_per_day"
            ]
            == 1
        )


def test_export_replays_recorded_availability_and_flags(db, session_factory, calendar, tmp_path):
    from datetime import timedelta

    from tests.factories import T0
    from tests.integration.agents.test_advisory import NOW, seed
    from trading.data.ingest import ingest_bars
    from trading.data.replay_provider import CsvReplayProvider
    from trading.services.heavy import export_recorded

    seed(db)
    db.execute(
        text("UPDATE market_bars SET quality_flags=ARRAY['DUPLICATE_CONFLICT'] WHERE ts=:t"),
        {"t": T0},
    )
    db.commit()
    info = calendar.session_on(T0.date())
    assert export_recorded(session_factory, tmp_path, provider="replay", info=info, as_of=NOW) > 0
    provider = CsvReplayProvider(
        tmp_path, name="recorded-replay", availability_clock=lambda: T0 + timedelta(minutes=1)
    )
    ingest_bars(
        db,
        provider,
        symbols=["SPY"],
        start=T0,
        end=T0 + timedelta(minutes=1),
        calendar=calendar,
        clock=ManualClock(T0 + timedelta(minutes=1)),
    )
    assert db.execute(
        text("SELECT quality_flags FROM market_bars WHERE provider='recorded-replay'")
    ).scalar_one() == ["DUPLICATE_CONFLICT"]
