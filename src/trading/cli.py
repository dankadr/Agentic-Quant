"""Command line entrypoint for all services and admin tools."""

import signal
import threading
from enum import StrEnum

import typer
import uvicorn
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import trading
from trading.api.app import create_app
from trading.api.auth import Role, create_principal
from trading.core.clock import SystemClock
from trading.core.config import AppConfig, Settings, load_config
from trading.db.engine import make_engine, make_session_factory
from trading.db.migrate import upgrade
from trading.jobs.lease import Lease
from trading.monitoring.logging import configure_logging
from trading.monitoring.metrics import start_metrics_server
from trading.services.handlers import WorkerContext
from trading.services.health import check_health
from trading.services.scheduler import run_scheduler
from trading.services.worker import run_worker

API_PORT = 8000
METRICS_PORTS = {"api": 9100, "worker": 9101, "scheduler": 9102}

app = typer.Typer(no_args_is_help=True, add_completion=False)
admin_app = typer.Typer(no_args_is_help=True, help="Administrative commands.")
app.add_typer(admin_app, name="admin")


class ServiceRole(StrEnum):
    WORKER = "worker"
    SCHEDULER = "scheduler"


def _bootstrap(service: str) -> tuple[Settings, AppConfig, sessionmaker[Session]]:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    config = load_config(settings.config_dir)
    start_metrics_server(settings.metrics_port or METRICS_PORTS[service])
    return settings, config, make_session_factory(make_engine(settings.resolved_database_url()))


def _stop_on_signals() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    return stop


@app.callback()
def main() -> None:
    """Paper-only trading system."""


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(trading.__version__)


@admin_app.command("create-api-key")
def create_api_key(
    name: str = typer.Option(..., help="Unique principal name."),
    role: Role = typer.Option(..., help="viewer (read-only) or operator."),
) -> None:
    """Create a principal and print its API key once, on stdout. The key is never stored."""
    engine = make_engine(Settings().resolved_database_url())
    try:
        with make_session_factory(engine).begin() as session:
            _, key = create_principal(session, name=name, role=role, now=SystemClock().now())
    except IntegrityError:
        typer.echo(f"principal {name!r} already exists", err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(key)


@app.command()
def api() -> None:
    """Run the FastAPI control API (no broker access)."""
    settings, config, session_factory = _bootstrap("api")
    application = create_app(
        settings=settings, config=config, session_factory=session_factory, clock=SystemClock()
    )
    # Binds all interfaces inside the container; Compose publishes the port on 127.0.0.1 only.
    uvicorn.run(application, host="0.0.0.0", port=API_PORT, log_config=None)  # noqa: S104


@app.command()
def worker() -> None:
    """Run the trading worker (the only process allowed to reach a broker adapter)."""
    settings, config, session_factory = _bootstrap("worker")
    clock = SystemClock()

    def ctx_factory(lease: Lease) -> WorkerContext:
        return WorkerContext(
            settings=settings, config=config, session_factory=session_factory, clock=clock,
            lease=lease,
        )  # fmt: skip

    code = run_worker(
        session_factory=session_factory,
        clock=clock,
        worker_id=settings.worker_id,
        ctx_factory=ctx_factory,
        stop=_stop_on_signals(),
    )
    raise typer.Exit(code)


@app.command()
def scheduler() -> None:
    """Run the single-leader scheduler."""
    settings, _config, session_factory = _bootstrap("scheduler")
    code = run_scheduler(
        session_factory=session_factory,
        clock=SystemClock(),
        holder=settings.worker_id,
        calendar=None,
        stop=_stop_on_signals(),
    )
    raise typer.Exit(code)


@app.command()
def migrate() -> None:
    """Apply database migrations (run with the owner credentials)."""
    settings = Settings()
    configure_logging(settings.log_level, settings.log_json)
    upgrade(settings.resolved_database_url())


@app.command()
def healthcheck(role: ServiceRole) -> None:
    """Exit 0 iff this service currently holds its own lease (used by container healthchecks)."""
    settings = Settings()
    engine = make_engine(settings.resolved_database_url())
    try:
        with make_session_factory(engine)() as session:
            healthy = check_health(role.value, session, settings, SystemClock())
    except SQLAlchemyError:
        healthy = False
    finally:
        engine.dispose()
    raise typer.Exit(0 if healthy else 1)
