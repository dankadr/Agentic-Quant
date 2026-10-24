from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.data.calendar import XnysCalendar
from trading.data.replay_provider import CsvReplayProvider
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease
from trading.quant.proposals import propose_config, review_proposal
from trading.services.bootstrap import build_runtime

CFG = load_config(Path("configs"))
MON, TUE = date(2026, 10, 19), date(2026, 10, 20)


def test_approved_parameters_take_effect_only_at_the_next_session_start(
    session_factory: sessionmaker[Session], db: Session, tmp_path: Path
) -> None:
    calendar = XnysCalendar(MON - timedelta(days=10), TUE + timedelta(days=10))
    info = calendar.session_on(MON)
    assert info is not None
    clock = ManualClock(info.open_at - timedelta(minutes=30))
    with session_factory.begin() as s:
        lease = acquire_lease(
            s, name=EXECUTOR_LEASE, holder="t", now=clock.now(), ttl=timedelta(days=2)
        )
    assert lease is not None
    provider = CsvReplayProvider(tmp_path)
    runtime = build_runtime(
        session_factory=session_factory,
        config=CFG,
        clock=clock,
        lease=lease,
        calendar=calendar,
        provider=provider,
        provider_name=provider.name,
    )

    def target_r() -> str:
        db.expire_all()
        return db.execute(
            text("select params_json->>'target_r' from strategies where id = 'orb_v1'")
        ).scalar_one()

    runtime.processor.start_session(MON)
    with session_factory.begin() as s:
        p = propose_config(
            s,
            strategy_id="orb_v1",
            base_version="1.0.0",
            parameters={"target_r": "2.0"},
            rationale="r",
            proposed_by="agent",
            universe={u.symbol for u in CFG.universe},
            now=clock.now(),
        )
        review_proposal(s, p.id, approve=True, reviewer="human", now=clock.now())

    runtime.processor.start_session(MON)  # a restart in the same session must not apply it
    assert target_r() == "1.5"

    clock.set(calendar.session_on(TUE).open_at - timedelta(minutes=30))  # type: ignore[union-attr]
    runtime.processor.start_session(TUE)
    assert target_r() == "2.0"
    assert (
        db.execute(
            text("select status from strategy_config_proposals where id = :i"), {"i": p.id}
        ).scalar_one()
        == "APPLIED"
    )
