"""FastAPI dependencies. Shared objects live on ``app.state`` (set in ``create_app``)."""

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from trading.core.clock import Clock
from trading.core.config import AppConfig


def get_session(request: Request) -> Iterator[Session]:
    """A transaction per request. Mutating routes commit explicitly before responding."""
    with request.app.state.session_factory.begin() as session:
        yield session


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_config(request: Request) -> AppConfig:
    config: AppConfig = request.app.state.config
    return config
