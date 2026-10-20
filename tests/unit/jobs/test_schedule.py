from datetime import UTC, datetime

from trading.jobs.schedule import plan_jobs


def _t(minute: int, second: int) -> datetime:
    return datetime(2026, 10, 19, 13, minute, second, tzinfo=UTC)


def test_plan_jobs_heartbeat_dedupe_per_minute() -> None:
    a = plan_jobs(_t(31, 5), None)
    b = plan_jobs(_t(31, 55), None)
    assert [j.dedupe_key for j in a] == [j.dedupe_key for j in b] == ["heartbeat:2026-10-19T13:31Z"]
    assert a[0].kind == "heartbeat"
    assert a[0].scheduled_at == _t(31, 0)
    assert a[0].payload == {}


def test_plan_jobs_new_minute_new_key() -> None:
    assert plan_jobs(_t(31, 59), None)[0].dedupe_key != plan_jobs(_t(32, 0), None)[0].dedupe_key
