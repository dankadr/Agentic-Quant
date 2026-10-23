from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.replay.helpers import BASIC, run, scalar, signal, write_script
from trading.control.state import RiskState, TradingState, get_system_state


def test_replay_is_deterministic(
    session_factory: sessionmaker[Session], db: Session, reset_db
) -> None:  # type: ignore[no-untyped-def]
    """Acceptance test 2: replaying a session twice yields identical order/fill/P&L hashes."""
    first = run(session_factory, BASIC)
    reset_db()
    second = run(session_factory, BASIC)
    assert first.result_hash == second.result_hash
    assert first == second
    assert (first.orders, first.fills) == (3, 3)
    assert first.decisions == 3


def test_hash_changes_when_inputs_change(
    session_factory: sessionmaker[Session], db: Session, reset_db, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    base = run(session_factory, BASIC)
    reset_db()
    variant = run(session_factory, write_script(tmp_path, signal("14:00", qty="0.16")))
    assert base.result_hash != variant.result_hash


def test_replay_ends_reconciled_and_normal(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    result = run(session_factory, BASIC)
    assert (
        scalar(
            session_factory,
            "select status from reconciliation_runs order by completed_at desc, id limit 1",
        )
        == "OK"
    )
    with session_factory() as s:
        state = get_system_state(s)
    assert (state.risk_state, state.trading_state) == (RiskState.NORMAL, TradingState.PAPER_RUNNING)
    statuses = {r[0] for r in _rows(session_factory, "select status from orders")}
    assert statuses == {"FILLED"}
    positions = dict(
        _rows(
            session_factory,
            "select i.symbol, p.qty from positions p join instruments i on i.id = p.instrument_id",
        )
    )
    assert positions == {"SPY": Decimal("0"), "QQQ": Decimal("0.10")}
    assert result.final_equity > Decimal("990")  # fees and spread are small


def _rows(sf: sessionmaker[Session], sql: str):  # type: ignore[no-untyped-def]
    with sf() as s:
        return [tuple(r) for r in s.execute(text(sql)).all()]


def test_session_end_records_unresolved_positions(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    run(session_factory, BASIC)
    details = scalar(
        session_factory, "select details_json from audit_events where action = 'session.end'"
    )
    assert details["unresolved_positions"] == {"QQQ": "0.1"}


def test_equity_snapshots_cover_the_session(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    run(session_factory, BASIC)
    kinds = dict(
        _rows(session_factory, "select kind, count(*) from equity_snapshots group by kind")
    )
    assert kinds["SESSION_START"] == 1
    assert kinds["SESSION_END"] == 1
    assert kinds["INTRADAY"] == 390


def test_every_order_references_a_decision_and_snapshot(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    run(session_factory, BASIC)
    assert (
        scalar(
            session_factory,
            "select count(*) from order_intents where risk_decision_id is null or market_snapshot_id is null",
        )
        == 0
    )
    assert (
        scalar(
            session_factory,
            "select count(*) from orders o join order_intents i on i.id = o.intent_id join risk_decisions d on d.id = i.risk_decision_id where d.allowed",
        )
        == 3
    )


def test_cash_and_fills_agree_with_the_venue(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    run(session_factory, BASIC)
    ledger = scalar(session_factory, "select sum(amount) from cash_ledger")
    venue = scalar(
        session_factory,
        "select a.starting_cash + coalesce(sum(case when f.side = 'SELL' then f.qty * f.price - f.fees else -(f.qty * f.price) - f.fees end), 0)"
        " from paper_venue.venue_accounts a left join paper_venue.venue_fills f on f.account_ref = a.account_ref group by a.starting_cash",
    )
    assert abs(ledger - venue) <= Decimal("0.0001")
