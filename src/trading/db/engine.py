"""Engine and session factory construction."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def make_engine(url: str) -> Engine:
    # Every connection runs in UTC so timestamps read back are UTC, never the server's zone.
    return create_engine(
        url, pool_pre_ping=True, future=True, connect_args={"options": "-c timezone=UTC"}
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
