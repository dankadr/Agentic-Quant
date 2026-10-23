from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from trading.api.app import create_app
from trading.api.auth import Role, create_principal
from trading.control.state import RiskState, set_risk_state
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.data.calendar import MarketCalendar
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease


@pytest.fixture
def app(
    session_factory: sessionmaker[Session], clock: ManualClock, calendar: MarketCalendar
) -> FastAPI:
    return create_app(
        settings=Settings(),
        config=load_config(Path("configs")),
        session_factory=session_factory,
        clock=clock,
        calendar=calendar,
    )


@pytest.fixture
def client(app: FastAPI, db: Session) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def make_key(
    session_factory: sessionmaker[Session], db: Session, clock: ManualClock
) -> Callable[[str, Role], str]:
    def _make(name: str, role: Role) -> str:
        with session_factory.begin() as s:
            _, key = create_principal(s, name=name, role=role, now=clock.now())
        return key

    return _make


@pytest.fixture
def operator_key(make_key: Callable[[str, Role], str]) -> str:
    return make_key("op1", Role.OPERATOR)


@pytest.fixture
def viewer_key(make_key: Callable[[str, Role], str]) -> str:
    return make_key("viewer1", Role.VIEWER)


@pytest.fixture
def make_ready(session_factory: sessionmaker[Session], clock: ManualClock) -> Callable[[], None]:
    """Executor lease held and risk state NORMAL, as after a clean worker start."""

    def _ready() -> None:
        with session_factory.begin() as s:
            acquire_lease(
                s, name=EXECUTOR_LEASE, holder="w1", now=clock.now(), ttl=timedelta(hours=1)
            )
            set_risk_state(
                s, risk_state=RiskState.NORMAL, reason="test", actor="t", now=clock.now()
            )

    return _ready


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}
