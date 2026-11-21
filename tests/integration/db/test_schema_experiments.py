"""Experiment values emitted by heavy services must work on the migrated schema."""

from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tests.factories import T0
from trading.db.migrate import _config, upgrade
from trading.db.models.research import Experiment
from trading.quant.research import record_experiment


def _record(db: Session, kind: str, status: str) -> Experiment:
    return record_experiment(
        db,
        kind=kind,
        status=status,
        hypothesis="schema regression",
        params={"window": 5},
        data_range={"source": "paper"},
        metrics={"count": 0},
        notes="retain history",
        now=T0,
    )


@pytest.mark.parametrize(
    "kind", ["BACKTEST", "WALK_FORWARD", "SENSITIVITY", "PAPER_COMPARISON", "RESEARCH_BATCH"]
)
@pytest.mark.parametrize("status", ["COMPLETED", "REJECTED", "INCONCLUSIVE", "SKIPPED"])
def test_record_experiment_accepts_service_values(db: Session, kind: str, status: str) -> None:
    row = _record(db, kind, status)
    db.expire_all()
    saved = db.execute(
        text("SELECT kind, status, params_json FROM experiments WHERE id = :id"), {"id": row.id}
    ).one()
    assert tuple(saved) == (kind, status, {"window": 5})


@pytest.mark.parametrize("kind,status", [("LIVE_TRADING", "COMPLETED"), ("BACKTEST", "RUNNING")])
def test_record_experiment_still_rejects_unknown_values(
    db: Session, kind: str, status: str
) -> None:
    with pytest.raises(IntegrityError, match=r"experiments_.*_check"):
        _record(db, kind, status)


@pytest.fixture
def previous_schema(database_url: str) -> Iterator[tuple[str, Engine]]:
    """Dedicated database: never downgrade the shared test database."""
    name = "experiment_schema_" + uuid4().hex
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
        had_role = conn.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = 'trading_app'"))
        if not had_role:
            conn.execute(text("CREATE ROLE trading_app"))
    url = admin.url.set(database=name).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        upgrade(url, "0011")
        yield url, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}"'))
            if not had_role:
                conn.execute(text("DROP ROLE trading_app"))
        admin.dispose()


def test_upgrade_and_safe_downgrade_preserve_history_and_grants(
    previous_schema: tuple[str, Engine],
) -> None:
    url, engine = previous_schema
    with Session(engine) as db, db.begin():
        row_id = _record(db, "WALK_FORWARD", "INCONCLUSIVE").id
    with engine.connect() as conn:
        before = conn.scalar(
            text("SELECT relacl::text FROM pg_class WHERE oid = 'experiments'::regclass")
        )
    assert before is not None and "trading_app" in before
    upgrade(url, "0012")
    with engine.connect() as conn:
        assert (
            conn.scalar(
                text("SELECT relacl::text FROM pg_class WHERE oid = 'experiments'::regclass")
            )
            == before
        )
    with Session(engine) as db:
        row = db.execute(
            text("SELECT kind, status, notes FROM experiments WHERE id = :id"), {"id": row_id}
        ).one()
        assert tuple(row) == ("WALK_FORWARD", "INCONCLUSIVE", "retain history")
    command.downgrade(_config(url), "0011")
    with Session(engine) as db:
        assert db.scalar(text("SELECT count(*) FROM experiments")) == 1
        with pytest.raises(IntegrityError):
            _record(db, "PAPER_COMPARISON", "COMPLETED")
    upgrade(url, "0012")


@pytest.mark.parametrize(
    "kind,status",
    [("PAPER_COMPARISON", "COMPLETED"), ("RESEARCH_BATCH", "COMPLETED"), ("BACKTEST", "SKIPPED")],
)
def test_downgrade_refuses_new_values_without_losing_history(
    previous_schema: tuple[str, Engine], kind: str, status: str
) -> None:
    url, engine = previous_schema
    upgrade(url, "0012")
    with Session(engine) as db, db.begin():
        row_id = _record(db, kind, status).id
    with pytest.raises(DBAPIError, match=r"Cannot downgrade.*experiment history"):
        command.downgrade(_config(url), "0011")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0012"
        assert conn.execute(
            text("SELECT kind, status FROM experiments WHERE id = :id"), {"id": row_id}
        ).one() == (kind, status)
    with Session(engine) as db, db.begin():
        _record(db, "RESEARCH_BATCH", "SKIPPED")


def test_short_history_prevents_lossy_downgrade(previous_schema: tuple[str, Engine]) -> None:
    url, engine = previous_schema
    upgrade(url)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO paper_venue.venue_accounts VALUES ('short-test', 1000)"))
        conn.execute(
            text(
                "INSERT INTO paper_venue.venue_orders (client_order_id, broker_order_id, account_ref,"
                "symbol, side, type, tif, qty, filled_qty, status, submitted_at, eligible_at, fill_seq,"
                "updated_at, position_effect) VALUES ('s','b','short-test','SPY','SELL','MARKET','DAY',"
                "1,0,'CANCELED',now(),now(),0,now(),'OPEN_SHORT')"
            )
        )
    with pytest.raises(RuntimeError, match="explicit short history"):
        command.downgrade(_config(url), "0012")
    with engine.connect() as conn:
        assert conn.scalar(text("SELECT version_num FROM alembic_version")) == "0013"
