from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import T0
from tests.replay.helpers import EXPIRED, run
from trading.execution.replay import ReplayError
from trading.portfolio.ledger import open_paper_account


def test_replay_refuses_a_database_that_holds_a_real_account(
    session_factory: sessionmaker[Session], db: Session
) -> None:
    """Review I8: replay must never run against the live database."""
    open_paper_account(db, name="paper-primary", starting_cash=Decimal("1000"), now=T0)
    db.commit()
    with pytest.raises(ReplayError, match="scratch"):
        run(session_factory, EXPIRED)
    assert db.execute(text("select count(*) from leases")).scalar_one() == 0  # nothing was taken


def test_replay_releases_the_executor_lease_when_it_crashes(
    session_factory: sessionmaker[Session], db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self, minute):  # type: ignore[no-untyped-def]
        raise RuntimeError("tick exploded")

    monkeypatch.setattr("trading.execution.tick.PaperTickProcessor.run_tick", boom)
    with pytest.raises(RuntimeError, match="tick exploded"):
        run(session_factory, EXPIRED)
    live = db.execute(text("select count(*) from leases where expires_at > now()")).scalar_one()
    assert live == 0  # a crashed replay must not lock the real worker out
