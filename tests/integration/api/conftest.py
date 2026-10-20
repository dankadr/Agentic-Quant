from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from trading.api.app import create_app
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config


@pytest.fixture
def app(session_factory: sessionmaker[Session], clock: ManualClock) -> FastAPI:
    return create_app(
        settings=Settings(),
        config=load_config(Path("configs")),
        session_factory=session_factory,
        clock=clock,
    )


@pytest.fixture
def client(app: FastAPI, db: Session) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
