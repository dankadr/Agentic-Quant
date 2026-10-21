"""Alembic environment. URL precedence: -x url=..., sqlalchemy.url option, then Settings."""

from alembic import context
from sqlalchemy import create_engine, pool

from trading.core.config import Settings
from trading.db import models  # noqa: F401
from trading.db.base import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    x_url = context.get_x_argument(as_dictionary=True).get("url")
    return x_url or config.get_main_option("sqlalchemy.url") or Settings().resolved_database_url()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
