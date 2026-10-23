"""A worker killed mid-session and restarted must finish exactly as an uninterrupted run would."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from tests.replay.helpers import BASIC, run


def test_restart_mid_session_matches_uninterrupted(
    session_factory: sessionmaker[Session], db: Session, reset_db
) -> None:  # type: ignore[no-untyped-def]
    uninterrupted = run(session_factory, BASIC)
    reset_db()

    # first process dies after the 16:00 tick (after the SPY and QQQ buys, before the SPY sell)
    run(session_factory, BASIC, stop_after=datetime(2026, 10, 19, 16, 0, tzinfo=UTC))
    # a brand-new process: new connections, new broker, new intent source; it reconciles first
    resumed = run(session_factory, BASIC, resume=True)

    assert resumed.result_hash == uninterrupted.result_hash
    assert (resumed.orders, resumed.fills) == (3, 3)


def test_restart_does_not_re_decide_old_signals(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    from tests.replay.helpers import scalar

    run(session_factory, BASIC, stop_after=datetime(2026, 10, 19, 16, 0, tzinfo=UTC))
    before = scalar(session_factory, "select count(*) from risk_decisions")
    run(session_factory, BASIC, resume=True)
    after = scalar(session_factory, "select count(*) from risk_decisions")
    assert (before, after) == (2, 3)  # only the 18:00 signal was decided after the restart
