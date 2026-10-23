from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from tests.replay.helpers import EXPIRED, GAPS, decision_reasons, run, scalar, signal, write_script


def until(hhmm: str) -> datetime:
    """Rejections are decided within minutes: stop the replay shortly after instead of all day."""
    hour, minute = hhmm.split(":")
    return datetime(2026, 10, 19, int(hour), int(minute), tzinfo=UTC)


def test_expired_signal_creates_no_order(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    """Acceptance test 3 (TTL)."""
    result = run(session_factory, EXPIRED, stop_after=until("14:10"))
    assert result.orders == 0
    assert decision_reasons(session_factory) == [[("SIGNAL_EXPIRED", "")]]
    assert scalar(session_factory, "select count(*) from order_intents") == 0


def test_stale_quotes_create_no_order(
    session_factory: sessionmaker[Session], db: Session, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Acceptance test 3 (stale quotes): the gaps fixture stops quoting at 15:00 UTC."""
    result = run(
        session_factory,
        write_script(tmp_path, signal("15:30")),
        fixtures=GAPS,
        stop_after=until("15:40"),
    )
    assert result.orders == 0
    (reasons,) = decision_reasons(session_factory)
    assert ("STALE_MARKET_DATA", "quote_age") in reasons


def test_exposure_limit_rejects_with_stable_code(
    session_factory: sessionmaker[Session], db: Session, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    """Acceptance test 4 end to end: 5 shares of ~$500 breaches the 10% symbol cap."""
    result = run(
        session_factory,
        write_script(tmp_path, signal("14:00", qty="5", limit="560.00")),
        stop_after=until("14:10"),
    )
    assert result.orders == 0
    (reasons,) = decision_reasons(session_factory)
    assert ("EXPOSURE_LIMIT", "symbol") in reasons


def test_shadow_strategy_cannot_trade(
    session_factory: sessionmaker[Session], db: Session, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    result = run(
        session_factory,
        write_script(tmp_path, signal("14:00", strategy="consensus_shadow")),
        stop_after=until("14:10"),
    )
    assert result.orders == 0
    (reasons,) = decision_reasons(session_factory)
    assert [code for code, _ in reasons] == ["STRATEGY_DISABLED"]


def test_no_new_entries_in_the_last_fifteen_minutes(
    session_factory: sessionmaker[Session], db: Session, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    late = signal(
        "19:50", symbol="QQQ", qty="0.1", limit="480.00"
    )  # a different symbol: no cap clash
    result = run(
        session_factory, write_script(tmp_path, late, signal("19:44")), stop_after=until("19:55")
    )
    assert result.orders == 1
    reasons = decision_reasons(session_factory)
    assert [("MARKET_CLOSED", "entry_cutoff")] in reasons


def test_wide_spread_is_rejected(
    session_factory: sessionmaker[Session], db: Session, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    # A quote file with a 1% spread: far beyond the 25 bps limit.
    (tmp_path / "bars.csv").write_text(
        "symbol,ts,open,high,low,close,volume,source_seq\n"
        + "".join(
            f"SPY,2026-10-19T{13 + (30 + m) // 60}:{(30 + m) % 60:02d}:00Z,500,500,500,500,10000,x{m}\n"
            for m in range(0, 90)
        )
    )
    (tmp_path / "quotes.csv").write_text(
        "symbol,ts,bid,ask,bid_size,ask_size\n"
        + "".join(
            f"SPY,2026-10-19T{13 + (30 + m) // 60}:{(30 + m) % 60:02d}:59Z,497.50,502.50,100,100\n"
            for m in range(0, 90)
        )
    )
    result = run(
        session_factory,
        write_script(tmp_path, signal("14:00"), name="s.yaml"),
        fixtures=tmp_path,
        stop_after=until("14:10"),
    )
    assert result.orders == 0
    (reasons,) = decision_reasons(session_factory)
    assert ("INVALID_QUOTE", "spread") in reasons
