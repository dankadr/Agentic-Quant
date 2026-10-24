from decimal import Decimal
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from tests.replay.helpers import CFG, DAY, rows_of, scalar
from tests.replay.orb_day import write_orb_day
from trading.core.config import AppConfig

D = Decimal


def orb_config(**params: object) -> AppConfig:
    orb = CFG.strategies["orb_v1"]
    merged = {**orb.params, "symbols": ["SPY"], **params}
    return CFG.model_copy(
        update={
            "strategies": {**CFG.strategies, "orb_v1": orb.model_copy(update={"params": merged})}
        }
    )


def replay(sf: sessionmaker[Session], fixtures: Path, config: AppConfig = CFG):  # type: ignore[no-untyped-def]
    from trading.execution.replay import replay_session

    return replay_session(sf, fixtures_dir=fixtures, session_date=DAY, script=None, config=config)


def test_orb_enters_on_the_breakout_and_exits_at_the_target(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    result = replay(session_factory, write_orb_day(tmp_path / "day"), orb_config())
    assert (result.orders, result.fills) == (2, 2)
    orders = rows_of(
        session_factory, "select side, status, qty, filled_qty from orders order by created_at"
    )
    assert [(o[0], o[1]) for o in orders] == [("BUY", "FILLED"), ("SELL", "FILLED")]
    assert orders[0][3] == orders[1][3]  # everything bought was sold
    codes = [
        r[0]
        for r in rows_of(
            session_factory,
            "select reason_codes from signals where status = 'ALLOCATED' order by generated_at",
        )
    ]
    assert codes[0] == ["ORB_BREAK", "VOLUME_CONFIRM"]
    assert codes[-1] == ["TARGET"]
    assert scalar(session_factory, "select qty from positions") == 0
    assert (
        scalar(session_factory, "select realized_pnl from positions") > 0
    )  # the rally paid for fees and spread


def test_attribution_records_the_round_trip_for_the_strategy(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    replay(session_factory, write_orb_day(tmp_path / "day"), orb_config())
    qty, fees, pnl = rows_of(
        session_factory,
        "select sum(allocated_qty), sum(allocated_fees), sum(pnl) from strategy_attribution where strategy_id = 'orb_v1'",
    )[0]
    assert qty == 0  # flat again
    assert fees > 0
    assert pnl > 0
    assert (
        scalar(
            session_factory,
            "select count(*) from strategy_attribution where strategy_id <> 'orb_v1'",
        )
        == 0
    )
    realized = scalar(session_factory, "select realized_pnl from positions")
    assert abs(pnl - realized) <= D("0.0002")  # attribution adds up to the ledger's P&L


def test_one_entry_per_session_even_though_price_stays_above_the_range(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    replay(session_factory, write_orb_day(tmp_path / "day"), orb_config())
    assert scalar(session_factory, "select count(*) from orders where side = 'BUY'") == 1


def test_positions_are_flattened_before_the_close_when_nothing_else_exits(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    config = orb_config(max_hold_minutes=900, target_r="50")  # no time stop, no reachable target
    result = replay(session_factory, write_orb_day(tmp_path / "day", rally_to=None), config)
    assert (result.orders, result.fills) == (2, 2)
    codes = [
        r[0]
        for r in rows_of(
            session_factory,
            "select reason_codes from signals where side = 'SELL' and status = 'ALLOCATED'",
        )
    ]
    assert codes[0] == ["EOD_FLATTEN"]
    assert scalar(session_factory, "select qty from positions") == 0
    details = scalar(
        session_factory, "select details_json from audit_events where action = 'session.end'"
    )
    assert details["unresolved_positions"] == {}  # nothing left open overnight
    sold_at = scalar(session_factory, "select min(created_at) from orders where side = 'SELL'")
    assert sold_at.hour == 19 and sold_at.minute == 50  # close 20:00 UTC minus the 10 minute window


def test_no_volume_confirmation_means_no_trade(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    result = replay(
        session_factory,
        write_orb_day(tmp_path / "day", breakout_volume=1000, later_volume=1000),
        orb_config(),
    )
    assert result.orders == 0
    assert scalar(session_factory, "select count(*) from signals") == 0


def test_strategy_replay_is_deterministic(
    session_factory: sessionmaker[Session], db: Session, reset_db, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    day = write_orb_day(tmp_path / "day")
    first = replay(session_factory, day, orb_config())
    reset_db()
    second = replay(session_factory, day, orb_config())
    assert first == second
    assert first.orders == 2


def test_every_strategy_order_passes_through_the_risk_engine(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    replay(session_factory, write_orb_day(tmp_path / "day"), orb_config())
    assert (
        scalar(
            session_factory,
            "select count(*) from orders o join order_intents i on i.id = o.intent_id join risk_decisions d on d.id = i.risk_decision_id where d.allowed",
        )
        == 2
    )
    reasons = scalar(
        session_factory,
        "select proposal_json->>'strategy_id' from risk_decisions where allowed order by ts limit 1",
    )
    assert reasons == "orb_v1"
