import json
from datetime import UTC, datetime
from pathlib import Path

from trading.api.app import create_app
from trading.core.clock import ManualClock
from trading.core.config import Settings, load_config
from trading.db.engine import make_engine, make_session_factory

SNAPSHOT = Path("docs/api-contracts/openapi.json")


def _schema() -> dict[str, object]:
    app = create_app(
        settings=Settings(),
        config=load_config(Path("configs")),
        session_factory=make_session_factory(
            make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
        ),
        clock=ManualClock(datetime(2026, 10, 19, tzinfo=UTC)),
    )
    return app.openapi()


def test_openapi_snapshot_is_current() -> None:
    """Regenerate with: uv run trading openapi > docs/api-contracts/openapi.json"""
    assert json.loads(SNAPSHOT.read_text()) == json.loads(json.dumps(_schema()))


def test_openapi_documents_the_v1_surface() -> None:
    paths = set(_schema()["paths"])  # type: ignore[call-overload]
    assert {
        "/v1/health/live", "/v1/health/ready", "/v1/system/status", "/v1/portfolio", "/v1/positions",
        "/v1/orders", "/v1/audit", "/v1/control/pause", "/v1/control/resume-paper",
    } <= paths  # fmt: skip
    assert not any("place_order" in p or "live" in p.replace("/v1/health/live", "") for p in paths)
