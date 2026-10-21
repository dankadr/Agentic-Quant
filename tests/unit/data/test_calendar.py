from datetime import UTC, date, datetime

import pytest

from trading.data.calendar import CalendarUnavailable, XnysCalendar

cal = XnysCalendar(date(2026, 1, 1), date(2027, 12, 31))


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_regular_day_edt() -> None:
    s = cal.session_on(date(2026, 10, 19))
    assert s is not None
    assert (s.open_at, s.close_at) == (utc("2026-10-19T13:30"), utc("2026-10-19T20:00"))
    assert not s.is_half_day
    assert s.session_date == date(2026, 10, 19)


def test_after_dst_end_est() -> None:
    s = cal.session_on(date(2026, 11, 2))
    assert s is not None
    assert (s.open_at, s.close_at) == (utc("2026-11-02T14:30"), utc("2026-11-02T21:00"))


def test_thanksgiving_closed() -> None:
    assert cal.session_on(date(2026, 11, 26)) is None


def test_black_friday_half_day() -> None:
    s = cal.session_on(date(2026, 11, 27))
    assert s is not None
    assert s.close_at == utc("2026-11-27T18:00")
    assert s.is_half_day


def test_christmas_eve_half_day_and_christmas_closed() -> None:
    eve = cal.session_on(date(2026, 12, 24))
    assert eve is not None
    assert eve.is_half_day
    assert eve.close_at == utc("2026-12-24T18:00")
    assert cal.session_on(date(2026, 12, 25)) is None


def test_weekend_none() -> None:
    assert cal.session_on(date(2026, 10, 24)) is None
    assert cal.session_on(date(2026, 10, 25)) is None


def test_session_at_open_inclusive_close_exclusive() -> None:
    assert cal.session_at(utc("2026-10-19T13:29:59")) is None
    assert cal.session_at(utc("2026-10-19T13:30:00")) is not None
    assert cal.session_at(utc("2026-10-19T19:59:59")) is not None
    assert cal.session_at(utc("2026-10-19T20:00:00")) is None


def test_session_at_requires_aware_utc_input() -> None:
    from trading.core.time import NaiveDatetimeError

    with pytest.raises(NaiveDatetimeError):
        cal.session_at(datetime(2026, 10, 19, 14, 0))  # noqa: DTZ001


def test_session_at_overnight_and_weekend_is_none() -> None:
    assert cal.session_at(utc("2026-10-20T03:00")) is None
    assert cal.session_at(utc("2026-10-24T15:00")) is None


def test_next_session_after_skips_weekend_and_holiday() -> None:
    assert cal.next_session_after(date(2026, 10, 23)).session_date == date(2026, 10, 26)
    assert cal.next_session_after(date(2026, 11, 25)).session_date == date(2026, 11, 27)


def test_settlement_t_plus_1_skips_weekend() -> None:
    assert cal.add_sessions(date(2026, 10, 23), 1) == date(2026, 10, 26)
    assert cal.add_sessions(date(2026, 10, 19), 1) == date(2026, 10, 20)
    assert cal.add_sessions(date(2026, 10, 19), 0) == date(2026, 10, 19)


def test_add_sessions_requires_a_session_date() -> None:
    with pytest.raises(ValueError, match="not a trading session"):
        cal.add_sessions(date(2026, 10, 24), 1)


def test_out_of_range_fails_closed() -> None:
    for call in (
        lambda: cal.session_on(date(2030, 1, 2)),
        lambda: cal.session_on(date(2025, 12, 31)),
        lambda: cal.session_at(utc("2030-01-02T15:00")),
        lambda: cal.next_session_after(date(2027, 12, 31)),
    ):
        with pytest.raises(CalendarUnavailable):
            call()
