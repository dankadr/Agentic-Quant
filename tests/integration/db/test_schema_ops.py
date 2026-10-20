from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

import trading.db.models  # noqa: F401
from trading.db.base import Base
from trading.db.migrate import upgrade

OPS_TABLES = {
    "jobs",
    "leases",
    "outbox",
    "audit_events",
    "system_state",
    "principals",
    "idempotency_keys",
}


def test_migration_creates_ops_tables(db: Session) -> None:
    assert OPS_TABLES <= set(inspect(db.get_bind()).get_table_names())


def test_orm_columns_match_database(db: Session) -> None:
    insp = inspect(db.get_bind())
    for name in OPS_TABLES:
        db_cols = {c["name"] for c in insp.get_columns(name)}
        assert db_cols == set(Base.metadata.tables[name].columns.keys()), name


def test_system_state_seeded(db: Session) -> None:
    row = db.execute(
        text("select mode, trading_state, risk_state, version from system_state")
    ).one()
    assert tuple(row) == ("PAPER_ONLY", "PAUSED", "UNKNOWN", 1)


def test_system_state_rejects_live_mode(db: Session) -> None:
    import pytest

    with pytest.raises(IntegrityError):
        db.execute(text("update system_state set mode='LIVE'"))


def test_system_state_is_singleton(db: Session) -> None:
    import pytest

    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "insert into system_state(id, mode, trading_state, risk_state, version, updated_at)"
                " values (2, 'PAPER_ONLY', 'PAUSED', 'UNKNOWN', 1, now())"
            )
        )


def _insert_audit(db: Session) -> None:
    db.execute(
        text(
            "insert into audit_events(actor, action, target, ts, result)"
            " values ('t', 'a', 'x', now(), 'ok')"
        )
    )


def test_audit_events_append_only_update(db: Session) -> None:
    import pytest

    _insert_audit(db)
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("update audit_events set result='changed'"))


def test_audit_events_append_only_delete(db: Session) -> None:
    import pytest

    _insert_audit(db)
    with pytest.raises(DBAPIError, match="append-only"):
        db.execute(text("delete from audit_events"))


def test_job_state_check(db: Session) -> None:
    import pytest

    with pytest.raises(IntegrityError):
        db.execute(
            text(
                "insert into jobs(kind, payload_json, state, scheduled_at, dedupe_key,"
                " created_at, updated_at) values ('k', '{}', 'BOGUS', now(), 'd', now(), now())"
            )
        )


def test_downgrade_upgrade_roundtrip(database_url: str) -> None:
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS roundtrip"))
        conn.execute(text("CREATE DATABASE roundtrip"))
    url = admin.url.set(database="roundtrip").render_as_string(hide_password=False)
    upgrade(url)
    upgrade(url, "base")
    upgrade(url)
    check = create_engine(url, poolclass=NullPool)
    with check.connect() as conn:
        assert OPS_TABLES <= set(inspect(conn).get_table_names())
    check.dispose()
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE roundtrip"))
    admin.dispose()
