from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trading.evaluation.holdout import HoldoutOverlap, consume

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def test_a_period_is_consumed_once_and_overlaps_are_refused(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        consume(s, start=date(2026, 1, 1), end=date(2026, 3, 31), purpose="exp-1",
                actor="ops", now=NOW)  # fmt: skip
    with pytest.raises(HoldoutOverlap) as inside:
        with session_factory.begin() as s:
            consume(s, start=date(2026, 3, 1), end=date(2026, 5, 1), purpose="exp-2",
                    actor="ops", now=NOW)  # fmt: skip
    assert inside.value.overlaps == [(date(2026, 1, 1), date(2026, 3, 31))]
    with pytest.raises(HoldoutOverlap):  # touching the last day counts as overlap
        with session_factory.begin() as s:
            consume(s, start=date(2026, 3, 31), end=date(2026, 4, 30), purpose="exp-3",
                    actor="ops", now=NOW)  # fmt: skip


def test_a_disjoint_period_is_accepted(db: Session, session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as s:
        consume(s, start=date(2026, 1, 1), end=date(2026, 1, 31), purpose="a", actor="ops", now=NOW)
    with session_factory.begin() as s:
        consume(s, start=date(2026, 2, 1), end=date(2026, 2, 28), purpose="b", actor="ops", now=NOW)
    db.rollback()
    assert db.execute(text("select count(*) from holdout_consumptions")).scalar_one() == 2


def test_the_consumption_record_cannot_be_rewritten(
    db: Session, session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as s:
        consume(s, start=date(2026, 1, 1), end=date(2026, 1, 31), purpose="a", actor="ops", now=NOW)
    with pytest.raises(DBAPIError):
        with session_factory.begin() as s:
            s.execute(text("update holdout_consumptions set purpose = 'nicer'"))
    db.rollback()
