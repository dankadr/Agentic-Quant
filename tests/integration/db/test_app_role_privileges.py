"""The application role, migrated from scratch. Other tests connect as the table owner and the test
database has no trading_app role, so the grants in the migrations are otherwise never exercised."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, make_url, text

from trading.db.migrate import upgrade

_DB = "app_role_check"
_ROLE = "trading_app"


@pytest.fixture
def migrated(database_url: str) -> Iterator[Engine]:
    admin = create_engine(database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP DATABASE IF EXISTS {_DB}"))
        c.execute(text(f"CREATE DATABASE {_DB}"))
        had_role = c.scalar(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": _ROLE})
        if not had_role:
            c.execute(text(f"CREATE ROLE {_ROLE}"))
    url = make_url(database_url).set(database=_DB).render_as_string(hide_password=False)
    engine = create_engine(url)
    try:
        upgrade(url)
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as c:
            c.execute(text(f"DROP DATABASE IF EXISTS {_DB}"))
            if not had_role:
                c.execute(text(f"DROP ROLE {_ROLE}"))
        admin.dispose()


def _can(engine: Engine, table: str, privilege: str) -> bool:
    with engine.connect() as c:
        return bool(
            c.scalar(
                text("SELECT has_table_privilege(:r, :t, :p)"),
                {"r": _ROLE, "t": table, "p": privilege},
            )
        )


def test_application_role_reads_the_schema_version_and_never_writes_it(migrated: Engine) -> None:
    assert _can(migrated, "alembic_version", "SELECT")  # /v1/system/status reports it
    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not _can(migrated, "alembic_version", privilege)


def test_application_role_can_use_agent_config_proposals(migrated: Engine) -> None:
    for privilege in ("SELECT", "INSERT", "UPDATE"):
        assert _can(migrated, "agent_config_proposals", privilege)
