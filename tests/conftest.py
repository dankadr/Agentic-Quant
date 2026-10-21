"""Shared fixtures. Integration tests run against a real PostgreSQL (testcontainers)."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.data.calendar import MarketCalendar
from trading.db.engine import make_engine, make_session_factory
from trading.db.migrate import upgrade
from trading.db.seed import seed_defaults

T0 = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
_DB_DIRS = ("integration", "replay", "chaos")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        parts = Path(str(item.fspath)).parts
        if any(d in parts for d in _DB_DIRS):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
    desktop_sock = Path.home() / ".docker" / "run" / "docker.sock"
    if "DOCKER_HOST" not in os.environ and desktop_sock.exists():
        os.environ["DOCKER_HOST"] = f"unix://{desktop_sock}"
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def session_factory(database_url: str) -> Iterator[sessionmaker[Session]]:
    upgrade(database_url)
    engine = make_engine(database_url)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(T0)


@pytest.fixture
def db(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        with session_factory.begin() as cleanup:
            tables = cleanup.execute(
                text(
                    "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) "
                    "FROM pg_tables WHERE schemaname IN ('public', 'paper_venue') "
                    "AND tablename <> 'alembic_version'"
                )
            ).scalars()
            names = ", ".join(tables)
            if names:
                cleanup.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
            seed_defaults(cleanup, T0)


@pytest.fixture(scope="session")
def calendar() -> "MarketCalendar":
    from datetime import date

    from trading.data.calendar import XnysCalendar

    return XnysCalendar(date(2026, 1, 1), date(2027, 12, 31))
