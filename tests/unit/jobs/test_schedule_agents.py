from datetime import UTC, date, datetime

from trading.core.config import AgentsConfig
from trading.data.calendar import MarketCalendar
from trading.jobs.schedule import plan_jobs, plan_session_jobs

DAY = date(2026, 10, 19)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _jobs(calendar: MarketCalendar, agents: AgentsConfig | None):  # type: ignore[no-untyped-def]
    info = calendar.session_on(DAY)
    assert info is not None
    return plan_session_jobs(info, agents=agents)


def test_no_agent_jobs_unless_enabled(calendar: MarketCalendar) -> None:
    assert not [j for j in _jobs(calendar, None) if j.kind.startswith("agent_")]
    assert not [j for j in _jobs(calendar, AgentsConfig()) if j.kind.startswith("agent_")]


def test_macro_runs_after_the_opening_range_then_every_interval_until_30_min_before_close(
    calendar: MarketCalendar,
) -> None:
    agents = AgentsConfig.model_validate({"macro": {"enabled": True}})
    jobs = [j for j in _jobs(calendar, agents) if j.kind == "agent_macro"]
    times = [j.scheduled_at for j in jobs]
    assert times[0] == _utc("2026-10-19T13:46:20")  # open + 16 min, after the 13:46 tick
    assert times[1] == _utc("2026-10-19T14:16:20")
    assert times[-1] == _utc("2026-10-19T19:16:20")  # the last one before 19:30 (close - 30 min)
    assert len({j.dedupe_key for j in jobs}) == len(jobs)
    assert jobs[0].dedupe_key == "agent_macro:2026-10-19T13:46:00Z"


def test_the_evaluator_runs_once_after_the_close(calendar: MarketCalendar) -> None:
    agents = AgentsConfig.model_validate({"evaluator": {"enabled": True}})
    (job,) = [j for j in _jobs(calendar, agents) if j.kind == "agent_evaluator"]
    assert job.scheduled_at == _utc("2026-10-19T20:10:00")
    assert job.payload == {"session_date": "2026-10-19"}


def test_plan_jobs_only_returns_agent_jobs_that_are_due(calendar: MarketCalendar) -> None:
    agents = AgentsConfig.model_validate({"macro": {"enabled": True}})
    due = plan_jobs(_utc("2026-10-19T13:46:30"), calendar, agents=agents)
    assert [j.kind for j in due if j.kind == "agent_macro"] == ["agent_macro"]
    assert not [
        j
        for j in plan_jobs(_utc("2026-10-19T13:50:00"), calendar, agents=agents)
        if j.kind == "agent_macro"
    ]
