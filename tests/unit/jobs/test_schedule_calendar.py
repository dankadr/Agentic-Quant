from datetime import UTC, date, datetime, timedelta

from trading.data.calendar import MarketCalendar
from trading.jobs.schedule import JobSpec, plan_jobs, plan_session_jobs


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _kinds(jobs: list[JobSpec], kind: str) -> list[JobSpec]:
    return [j for j in jobs if j.kind == kind]


def _session_jobs(calendar: MarketCalendar, d: date) -> list[JobSpec]:
    info = calendar.session_on(d)
    assert info is not None
    return plan_session_jobs(info)


def test_full_day_tick_count(calendar: MarketCalendar) -> None:
    assert len(_kinds(_session_jobs(calendar, date(2026, 10, 19)), "paper_tick")) == 390


def test_half_day_tick_count(calendar: MarketCalendar) -> None:
    assert len(_kinds(_session_jobs(calendar, date(2026, 11, 27)), "paper_tick")) == 210


def test_session_job_times(calendar: MarketCalendar) -> None:
    jobs = _session_jobs(calendar, date(2026, 10, 19))
    (start,) = _kinds(jobs, "session_start")
    (end,) = _kinds(jobs, "session_end")
    ticks = _kinds(jobs, "paper_tick")
    assert (start.scheduled_at, start.dedupe_key) == (
        _utc("2026-10-19T13:00"),
        "session_start:2026-10-19",
    )
    assert start.payload == {"session_date": "2026-10-19"}
    assert (end.scheduled_at, end.dedupe_key) == (
        _utc("2026-10-19T20:05"),
        "session_end:2026-10-19",
    )
    assert ticks[0].payload == {"minute": "2026-10-19T13:31:00Z"}
    assert ticks[0].scheduled_at == _utc("2026-10-19T13:31:05")  # after the bar closed
    assert ticks[-1].payload == {"minute": "2026-10-19T20:00:00Z"}
    assert ticks[-1].scheduled_at == _utc("2026-10-19T20:00:05")


def test_dedupe_keys_are_unique_and_stable(calendar: MarketCalendar) -> None:
    a = [j.dedupe_key for j in _session_jobs(calendar, date(2026, 10, 19))]
    b = [j.dedupe_key for j in _session_jobs(calendar, date(2026, 10, 19))]
    assert a == b
    assert len(set(a)) == len(a) == 392


def test_holiday_and_weekend_plan_only_a_heartbeat(calendar: MarketCalendar) -> None:
    for day in ("2026-11-26T15:00:30", "2026-10-24T15:00:30"):
        assert [j.kind for j in plan_jobs(_utc(day), calendar)] == ["heartbeat"]


def test_only_recent_jobs_are_planned_and_missed_ticks_are_not_backfilled(
    calendar: MarketCalendar,
) -> None:
    now = _utc("2026-10-19T15:00:30")
    jobs = plan_jobs(now, calendar)
    ticks = [j.payload["minute"] for j in _kinds(jobs, "paper_tick")]
    assert ticks == ["2026-10-19T14:59:00Z", "2026-10-19T15:00:00Z"]
    assert all(now - timedelta(minutes=2) <= j.scheduled_at <= now for j in jobs)
    assert not _kinds(jobs, "session_start")
    assert not _kinds(jobs, "session_end")


def test_session_start_planned_shortly_after_its_scheduled_time(calendar: MarketCalendar) -> None:
    assert len(_kinds(plan_jobs(_utc("2026-10-19T13:01:00"), calendar), "session_start")) == 1
    assert not _kinds(plan_jobs(_utc("2026-10-19T13:10:00"), calendar), "session_start")


def test_session_end_planned_after_close(calendar: MarketCalendar) -> None:
    assert len(_kinds(plan_jobs(_utc("2026-10-19T20:06:00"), calendar), "session_end")) == 1


def test_calendar_out_of_range_fails_closed_to_heartbeat_only(calendar: MarketCalendar) -> None:
    assert [j.kind for j in plan_jobs(_utc("2035-03-01T15:00:00"), calendar)] == ["heartbeat"]


def test_no_calendar_is_heartbeat_only() -> None:
    assert [j.kind for j in plan_jobs(_utc("2026-10-19T15:00:00"), None)] == ["heartbeat"]
