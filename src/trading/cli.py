"""Command line entrypoint for all services and admin tools."""

import json
import signal
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path

import typer
import uvicorn
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

import trading
from trading.api.app import create_app
from trading.api.auth import Role, create_principal
from trading.core.clock import SystemClock
from trading.core.config import AppConfig, Settings, load_config
from trading.core.time import ensure_utc
from trading.data.calendar import XnysCalendar
from trading.data.ingest import ingest_bars, ingest_corporate_actions, ingest_quotes
from trading.data.instruments import upsert_universe
from trading.data.replay_provider import CsvReplayProvider
from trading.db.engine import make_engine, make_session_factory
from trading.db.migrate import upgrade
from trading.execution.replay import ReplayError, replay_session
from trading.jobs.lease import Lease
from trading.monitoring.logging import configure_logging
from trading.monitoring.metrics import start_metrics_server
from trading.risk.halts import NotInHaltError, clear_drawdown_halt
from trading.services.bootstrap import build_runtime
from trading.services.handlers import WorkerContext, reconcile_on_start
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


def _calendar() -> XnysCalendar:
    today = SystemClock().now().date()
    return XnysCalendar(today - timedelta(days=30), today + timedelta(days=400))


@app.command()
def worker() -> None:
    """Run the trading worker (the only process allowed to reach a broker adapter)."""
    settings, config, session_factory = _bootstrap("worker")
    clock = SystemClock()
    calendar = _calendar()

    def ctx_factory(lease: Lease) -> WorkerContext:
        # No market-data provider is configured yet (open decision 1): ticks are no-ops until then.
        runtime = build_runtime(
            session_factory=session_factory,
            config=config,
            clock=clock,
            lease=lease,
            calendar=calendar,
            provider=None,
        )
        return WorkerContext(
            settings=settings,
            config=config,
            session_factory=session_factory,
            clock=clock,
            lease=lease,
            runtime=runtime,
        )

    code = run_worker(
        session_factory=session_factory,
        clock=clock,
        worker_id=settings.worker_id,
        ctx_factory=ctx_factory,
        stop=_stop_on_signals(),
        startup_hooks=[reconcile_on_start],
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
        calendar=_calendar(),
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


def _parse_utc(value: str, option: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option}: invalid ISO-8601 timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise typer.BadParameter(f"{option}: naive timestamp {value!r}; a timezone is required")
    return ensure_utc(parsed)


@app.command("ingest-csv")
def ingest_csv(
    path: Path = typer.Option(..., help="Directory with bars.csv / quotes.csv."),
    start: str = typer.Option(..., help="Inclusive start, ISO-8601 with timezone."),
    end: str = typer.Option(..., help="Exclusive end, ISO-8601 with timezone."),
) -> None:
    """Load CSV bars, quotes and corporate actions for the configured universe."""
    start_dt, end_dt = _parse_utc(start, "--start"), _parse_utc(end, "--end")
    settings = Settings()
    config = load_config(settings.config_dir)
    calendar = XnysCalendar(start_dt.date() - timedelta(days=7), end_dt.date() + timedelta(days=7))
    provider = CsvReplayProvider(path)
    symbols = [u.symbol for u in config.universe]
    clock = SystemClock()
    engine = make_engine(settings.resolved_database_url())
    try:
        with make_session_factory(engine).begin() as session:
            upsert_universe(session, config.universe)
            bars = ingest_bars(
                session, provider, symbols=symbols, start=start_dt, end=end_dt,
                calendar=calendar, clock=clock,
            )  # fmt: skip
            quotes = ingest_quotes(
                session, provider, symbols=symbols, start=start_dt, end=end_dt,
                calendar=calendar, clock=clock,
            )  # fmt: skip
            actions = ingest_corporate_actions(
                session, provider, symbols=symbols, start=start_dt.date(), end=end_dt.date(),
                clock=clock,
            )  # fmt: skip
    finally:
        engine.dispose()
    typer.echo(
        json.dumps(
            {
                "bars": bars.to_dict(),
                "quotes": quotes.to_dict(),
                "corporate_actions": actions.to_dict(),
            }
        )
    )


@admin_app.command("clear-drawdown-halt")
def clear_drawdown_halt_command(
    reason: str = typer.Option(..., help="Why the halt is being cleared (audited)."),
) -> None:
    """Clear a HALTED_DRAWDOWN risk state after review. Resets peak equity; always audited."""
    engine = make_engine(Settings().resolved_database_url())
    try:
        with make_session_factory(engine).begin() as session:
            state = clear_drawdown_halt(
                session,
                actor="operator-cli",
                reason=reason,
                request_id=None,
                now=SystemClock().now(),
            )
    except NotInHaltError:
        typer.echo("risk state is not HALTED_DRAWDOWN; nothing to clear", err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(json.dumps({"risk_state": state.risk_state.value, "version": state.version}))


@app.command()
def replay(
    fixtures: Path = typer.Option(..., help="Directory with bars.csv / quotes.csv."),
    session_date: str = typer.Option(..., "--date", help="Session date, YYYY-MM-DD."),
    script: Path | None = typer.Option(None, help="YAML of scripted proposals."),
) -> None:
    """Replay one recorded session deterministically (use a scratch database)."""
    settings = Settings()
    config = load_config(settings.config_dir)
    engine = make_engine(settings.resolved_database_url())
    try:
        result = replay_session(
            make_session_factory(engine),
            fixtures_dir=fixtures,
            session_date=date.fromisoformat(session_date),
            script=script,
            config=config,
        )
    except ReplayError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(
        json.dumps(
            {k: str(v) if not isinstance(v, int | str) else v for k, v in asdict(result).items()}
        )
    )


@app.command()
def openapi() -> None:
    """Print the OpenAPI schema (no database needed)."""
    application = create_app(
        settings=Settings(),
        config=load_config(Settings().config_dir),
        session_factory=make_session_factory(
            make_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
        ),
        clock=SystemClock(),
    )
    typer.echo(json.dumps(application.openapi(), indent=2, sort_keys=True))
