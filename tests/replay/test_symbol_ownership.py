from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from tests.replay.helpers import rows_of, run, signal, write_script
from tests.replay.test_replay_rejections import until


def test_stale_foreign_proposal_does_not_abort_other_tickers(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    script = write_script(
        tmp_path,
        signal("14:00", qty="0.05"),
        signal("14:01", qty="0.05", strategy="vwap_v1"),
        signal("14:02", symbol="QQQ", qty="0.05", limit="480.00"),
    )
    result = run(session_factory, script, stop_after=until("14:04"))
    assert result.orders == 2
    assert rows_of(
        session_factory,
        "select i.symbol,n.strategy_ids from orders o join order_intents n on n.id=o.intent_id "
        "join instruments i on i.id=o.instrument_id order by o.created_at",
    ) == [("SPY", ["orb_v1"]), ("QQQ", ["orb_v1"])]
