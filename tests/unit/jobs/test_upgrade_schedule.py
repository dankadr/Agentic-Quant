from datetime import UTC, date, datetime
from types import SimpleNamespace

from trading.core.config import AgentsConfig
from trading.jobs.schedule import plan_jobs, plan_session_jobs


def enabled():
    base = AgentsConfig()
    return SimpleNamespace(
        **{
            **base.__dict__,
            **{
                n: SimpleNamespace(enabled=True)
                for n in ("data_quality", "execution_quality", "opportunities", "technical")
            },
        }
    )


def test_advisory_and_comparison_schedule(calendar):
    info = calendar.session_on(date(2026, 10, 19))
    jobs = plan_session_jobs(info, agents=enabled())
    quality = [j for j in jobs if j.kind == "agent_data_quality"]
    opportunities = [j for j in jobs if j.kind == "agent_opportunities"]
    assert len(quality) == 78
    assert quality[0].scheduled_at == datetime(2026, 10, 19, 13, 35, 20, tzinfo=UTC)
    assert opportunities[0].scheduled_at == datetime(2026, 10, 19, 14, 0, 20, tzinfo=UTC)
    assert len(opportunities) == 12
    assert any(j.kind == "agent_execution_quality" for j in jobs)
    assert any(j.kind == "paper_comparison" for j in jobs)
    assert len({j.dedupe_key for j in jobs}) == len(jobs)


def test_weekly_research_has_week_key_and_no_trading_jobs_on_weekend(calendar):
    now = datetime(2026, 11, 14, 8, 0, 30, tzinfo=UTC)
    jobs = plan_jobs(now, calendar, agents=enabled())
    research = [j for j in jobs if j.kind == "quant_research"]
    assert len(research) == 1
    assert research[0].dedupe_key == "quant_research:2026-11-14"
    assert not [j for j in jobs if j.kind == "paper_tick"]
