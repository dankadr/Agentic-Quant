"""Trust boundaries from the spec: the API process can never reach a broker."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = ("trading.brokers", "trading.execution.manager", "trading.services.bootstrap")


def _imports(path: Path) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_api_modules_do_not_import_brokers_or_the_execution_manager() -> None:
    offenders = [
        f"{path.relative_to(ROOT)}: {name}"
        for path in (ROOT / "src" / "trading" / "api").rglob("*.py")
        for name in _imports(path)
        if name.startswith(FORBIDDEN)
    ]
    assert offenders == []


def test_importing_the_api_never_loads_broker_code() -> None:
    """The transitive closure, not just direct imports."""
    code = (
        "import sys, trading.api.app\n"
        "bad = sorted(m for m in sys.modules if m.startswith(('trading.brokers', 'trading.execution.manager', 'trading.services.bootstrap')))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
        env={"PYTHONPATH": str(ROOT / "src")},
    )
    assert out.stdout.strip() == ""


def test_no_module_outside_the_worker_constructs_a_broker() -> None:
    allowed = {"src/trading/services/bootstrap.py", "src/trading/brokers/paper/broker.py"}
    constructors = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "src" / "trading").rglob("*.py")
        if "PaperBroker(" in path.read_text() and str(path.relative_to(ROOT)) not in allowed
    ]
    assert constructors == []


def test_strategy_and_agent_packages_cannot_import_brokers() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for pkg in ("quant", "agents", "mcp")
        for path in (ROOT / "src" / "trading" / pkg).rglob("*.py")
        if any(n.startswith("trading.brokers") for n in _imports(path))
    ]
    assert offenders == []


def test_only_pause_and_resume_mutate_state_through_the_api(app: FastAPI) -> None:
    """Acceptance test 9/10 groundwork: no route can place orders, change risk or go live."""
    paths = app.openapi()["paths"]
    mutating = {
        (method.upper(), path)
        for path, operations in paths.items()
        for method in operations
        if method.lower() not in {"get", "head", "options"}
    }
    assert mutating == {("POST", "/v1/control/pause"), ("POST", "/v1/control/resume-paper")}
    assert len(paths) >= 9  # guard against the check passing because the schema came back empty


@pytest.fixture
def app() -> FastAPI:
    from datetime import UTC, datetime

    from trading.api.app import create_app
    from trading.core.clock import ManualClock
    from trading.core.config import Settings, load_config
    from trading.db.engine import make_engine, make_session_factory

    return create_app(
        settings=Settings(),
        config=load_config(Path("configs")),
        session_factory=make_session_factory(
            make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
        ),
        clock=ManualClock(datetime(2026, 10, 19, tzinfo=UTC)),
    )
