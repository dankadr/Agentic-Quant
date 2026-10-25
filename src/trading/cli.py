"""Command line entrypoint for all services and admin tools."""

import json
import signal
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import typer
import uvicorn
from sqlalchemy import text
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
from trading.data.provider import MarketDataProvider
from trading.data.replay_provider import CsvReplayProvider
from trading.db.engine import make_engine, make_session_factory
from trading.db.migrate import upgrade
from trading.jobs.lease import Lease
from trading.monitoring.logging import configure_logging
from trading.monitoring.metrics import start_metrics_server
from trading.risk.halts import NotInHaltError, clear_drawdown_halt
from trading.services.health import check_health, new_holder_id

API_PORT = 8000
METRICS_PORTS = {"api": 9100, "worker": 9101, "scheduler": 9102}

if TYPE_CHECKING:
    from trading.agents.llm import LlmClient
    from trading.data.alpaca_news import AlpacaNewsSource
    from trading.data.alpaca_provider import AlpacaDataProvider

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


def _llm_client(
    settings: Settings, config: AppConfig, *, force: bool = False
) -> "LlmClient | None":
    """The research-agent model client, if any agent is enabled and a key file is present."""
    from trading.agents.llm import LlmClient  # noqa: PLC0415

    any_agent = config.agents.macro.enabled or config.agents.evaluator.enabled
    any_agent = any_agent or config.agents.fundamental.enabled
    if not (any_agent or force):
        return None
    key_file = settings.llm_api_key_file or Path("secrets/llm_api_key")
    if not (key_file.exists() and key_file.read_text().strip()):
        return None
    return LlmClient.from_file(config.agents.llm, key_file)


def _news_source(settings: Settings, config: AppConfig) -> "AlpacaNewsSource | None":
    """Alpaca news (same data-only keys) when the fundamental agent is enabled."""
    from trading.data.alpaca_news import AlpacaNewsSource  # noqa: PLC0415

    if not config.agents.fundamental.enabled:
        return None
    key = settings.alpaca_key_id_file or Path("secrets/alpaca_key_id")
    secret = settings.alpaca_secret_file or Path("secrets/alpaca_secret")
    if not (
        key.exists() and secret.exists() and key.read_text().strip() and secret.read_text().strip()
    ):
        return None
    return AlpacaNewsSource.from_files(key, secret)


@app.command()
def worker() -> None:
    """Run the trading worker (the only process allowed to reach a broker adapter)."""
    # Imported here, not at module level: the API and scheduler containers run this same CLI
    # and must never load broker code.
    from trading.services.bootstrap import build_runtime  # noqa: PLC0415
    from trading.services.handlers import WorkerContext, reconcile_on_start  # noqa: PLC0415
    from trading.services.worker import run_worker  # noqa: PLC0415

    settings, config, session_factory = _bootstrap("worker")
    clock = SystemClock()
    calendar = _calendar()
    provider = _alpaca_provider(settings)
    llm = _llm_client(settings, config)
    news = _news_source(settings, config)

    def ctx_factory(lease: Lease) -> WorkerContext:
        # Without Alpaca data keys ticks are no-ops (fail closed); with them the worker trades
        # PAPER ONLY on live IEX data.
        runtime = build_runtime(
            session_factory=session_factory,
            config=config,
            clock=clock,
            lease=lease,
            calendar=calendar,
            provider=provider,
            provider_name=provider.name if provider is not None else "replay",
        )
        return WorkerContext(
            settings=settings,
            config=config,
            session_factory=session_factory,
            clock=clock,
            lease=lease,
            runtime=runtime,
            llm=llm,
            news=news,
        )

    code = run_worker(
        session_factory=session_factory,
        clock=clock,
        worker_id=new_holder_id(settings.worker_id),
        ctx_factory=ctx_factory,
        stop=_stop_on_signals(),
        startup_hooks=[reconcile_on_start],
    )
    raise typer.Exit(code)


@app.command()
def scheduler() -> None:
    """Run the single-leader scheduler."""
    from trading.services.scheduler import run_scheduler  # noqa: PLC0415

    settings, _config, session_factory = _bootstrap("scheduler")
    code = run_scheduler(
        session_factory=session_factory,
        clock=SystemClock(),
        holder=new_holder_id(settings.worker_id),
        calendar=_calendar(),
        stop=_stop_on_signals(),
        agents=_config.agents,
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


def _alpaca_key_files(settings: Settings) -> tuple[Path, Path] | None:
    """The Alpaca data key files if both exist and are non-empty (empty = not configured)."""
    key = settings.alpaca_key_id_file or Path("secrets/alpaca_key_id")
    secret = settings.alpaca_secret_file or Path("secrets/alpaca_secret")
    if not (key.exists() and secret.exists()):
        return None
    if not (key.read_text().strip() and secret.read_text().strip()):
        return None
    return key, secret


def _alpaca_provider(settings: Settings, *, workers: int = 1) -> "AlpacaDataProvider | None":
    """The data-only Alpaca provider if its key files are configured (or in ./secrets)."""
    from trading.data.alpaca_provider import AlpacaDataProvider  # noqa: PLC0415

    files = _alpaca_key_files(settings)
    return AlpacaDataProvider.from_files(*files, workers=workers) if files else None


@app.command()
def report(
    session_date: str = typer.Option(..., "--date", help="Session date, YYYY-MM-DD."),
    provider: str = typer.Option("alpaca-iex", help="Market-data provider name to report on."),
) -> None:
    """Print the end-of-day report: data coverage, strategy activity, costs and P&L."""
    from trading.monitoring.report import daily_report  # noqa: PLC0415

    settings = Settings()
    engine = make_engine(settings.resolved_database_url())
    try:
        with make_session_factory(engine)() as session:
            body = daily_report(
                session,
                date.fromisoformat(session_date),
                config=load_config(settings.config_dir),
                provider=provider,
            )
    finally:
        engine.dispose()
    typer.echo(json.dumps(body, indent=2))


@app.command()
def research(
    strategy: str = typer.Option(..., help="orb_v1 or vwap_v1."),
    fixtures: Path = typer.Option(..., help="Merged bars.csv/quotes.csv (trading collect-data)."),
    train: int = typer.Option(10, min=2),
    test: int = typer.Option(5, min=1),
    holdout: int = typer.Option(5, min=0),
    minutes_per_day: int | None = typer.Option(None, min=1, help="Shorten sessions (debug)."),
    evaluate_holdout: bool = typer.Option(False, help="Spend the single-use final holdout."),
    cache_dir: Path = typer.Option(
        Path("data/research-cache"), help="Resume store for finished runs."
    ),
) -> None:
    """Walk-forward + sensitivity for one strategy. Backtests run on TRADING_SCRATCH_DATABASE_URL
    (wiped!); experiments are recorded in TRADING_DATABASE_URL."""
    from trading.data.collect import merged_dates  # noqa: PLC0415
    from trading.quant.backtest import backtest_full_run  # noqa: PLC0415
    from trading.quant.research_config import load_research_spaces  # noqa: PLC0415
    from trading.quant.research_runner import (  # noqa: PLC0415
        HoldoutAlreadyUsed,
        disk_cached,
        run_research,
    )

    settings = Settings()
    config = load_config(settings.config_dir)
    spaces = load_research_spaces(settings.config_dir)
    if strategy not in spaces or strategy not in config.strategies:
        raise typer.BadParameter(f"no research grid for {strategy!r} in configs/research.yaml")
    if not settings.scratch_database_url or settings.scratch_database_url == (
        settings.database_url or settings.resolved_database_url()
    ):
        typer.echo("set TRADING_SCRATCH_DATABASE_URL to a SEPARATE scratch database", err=True)
        raise typer.Exit(1)
    space = spaces[strategy]
    params = config.strategies[strategy].params
    base = {k: params[k] for k in space}
    neighbors = {k: [v for v in vals if str(v) != str(base[k])] for k, vals in space.items()}
    dates = merged_dates(fixtures)
    scratch, live = (
        make_engine(settings.scratch_database_url),
        make_engine(settings.resolved_database_url()),
    )
    try:
        with make_session_factory(live)() as session:
            version = session.execute(
                text("SELECT version FROM strategies WHERE id = :s"), {"s": strategy}
            ).scalar()
        outcome = run_research(
            make_session_factory(live),
            disk_cached(
                backtest_full_run(
                    make_session_factory(scratch),
                    fixtures_dir=fixtures,
                    config=config,
                    strategy_id=strategy,
                    dates=dates,
                    minutes_per_day=minutes_per_day,
                ),
                cache_dir,
                {
                    "strategy": strategy,
                    "strategy_params": dict(config.strategies[strategy].params),
                    "dates": [d.isoformat() for d in dates],
                    "bars_size": (fixtures / "bars.csv").stat().st_size,
                    "quotes_size": (fixtures / "quotes.csv").stat().st_size,
                    "minutes_per_day": minutes_per_day,
                },
            ),
            strategy_id=strategy,
            dates=dates,
            base_params=base,
            space=space,
            neighbors=neighbors,
            train=train,
            test=test,
            holdout=holdout,
            base_equity=config.system.starting_cash_usd,
            now=SystemClock().now(),
            evaluate_holdout=evaluate_holdout,
            strategy_version=version,
        )
    except HoldoutAlreadyUsed as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        scratch.dispose()
        live.dispose()
    typer.echo(
        json.dumps(
            {
                "walk_forward": outcome.walk_forward.verdict,
                "oos_net_pnl": str(outcome.walk_forward.out_of_sample_net_pnl),
                "sensitivity_robust": outcome.sensitivity.robust,
                "holdout_net_pnl": str(outcome.holdout.net_pnl) if outcome.holdout else None,
            }
        )
    )


@app.command()
def technical(
    strategy: str = typer.Option(..., help="orb_v1 or vwap_v1."),
) -> None:
    """Ask the technical agent to review the latest research for a strategy (may file a proposal
    that a human must still approve)."""
    from trading.agents.technical import run_technical_agent  # noqa: PLC0415

    settings = Settings()
    config = load_config(settings.config_dir)
    llm = _llm_client(settings, config, force=True)
    if llm is None:
        typer.echo("no LLM key (secrets/llm_api_key)", err=True)
        raise typer.Exit(1)
    engine = make_engine(settings.resolved_database_url())
    try:
        status = run_technical_agent(
            make_session_factory(engine), llm, config=config, strategy_id=strategy,
            now=SystemClock().now(),
        )  # fmt: skip
    finally:
        engine.dispose()
        llm.close()
    typer.echo(status)


@app.command("alpaca-check")
def alpaca_check(
    symbol: str = typer.Option("SPY"),
    start: str = typer.Option(..., help="Inclusive start, ISO-8601 with timezone."),
    end: str = typer.Option(..., help="Exclusive end, ISO-8601 with timezone."),
) -> None:
    """Fetch a short window of Alpaca bars and quotes and print a summary (no database)."""
    from trading.data.alpaca_provider import ProviderError  # noqa: PLC0415

    provider = _alpaca_provider(Settings())
    if provider is None:
        typer.echo(
            "no Alpaca key files found (secrets/alpaca_key_id, secrets/alpaca_secret)", err=True
        )
        raise typer.Exit(1)
    start_dt, end_dt = _parse_utc(start, "--start"), _parse_utc(end, "--end")
    try:
        bars = list(provider.fetch_bars([symbol], start_dt, end_dt))
        quotes = list(provider.fetch_quotes([symbol], start_dt, end_dt))
    except ProviderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        provider.close()
    typer.echo(
        json.dumps(
            {
                "bars": len(bars),
                "first_bar": bars[0].model_dump(mode="json") if bars else None,
                "quotes": len(quotes),
                "first_quote": quotes[0].model_dump(mode="json") if quotes else None,
            }
        )
    )


def _ingest(provider: MarketDataProvider, start: str, end: str, *, quotes: bool = True) -> None:
    start_dt, end_dt = _parse_utc(start, "--start"), _parse_utc(end, "--end")
    settings = Settings()
    config = load_config(settings.config_dir)
    calendar = XnysCalendar(start_dt.date() - timedelta(days=7), end_dt.date() + timedelta(days=7))
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
            quote_summary = (
                ingest_quotes(
                    session,
                    provider,
                    symbols=symbols,
                    start=start_dt,
                    end=end_dt,
                    calendar=calendar,
                    clock=clock,
                )
                if quotes
                else None
            )
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
                "quotes": quote_summary.to_dict() if quote_summary else None,
                "corporate_actions": actions.to_dict(),
            }
        )
    )


@app.command("ingest-csv")
def ingest_csv(
    path: Path = typer.Option(..., help="Directory with bars.csv / quotes.csv."),
    start: str = typer.Option(..., help="Inclusive start, ISO-8601 with timezone."),
    end: str = typer.Option(..., help="Exclusive end, ISO-8601 with timezone."),
) -> None:
    """Load CSV bars, quotes and corporate actions for the configured universe."""
    _ingest(CsvReplayProvider(path), start, end)


@app.command("collect-data")
def collect_data(
    days: int = typer.Option(30, min=1, help="How many completed sessions, counting back."),
    out: Path = typer.Option(Path("data/alpaca"), help="Per-session directories go here."),
    quotes: bool = typer.Option(True, help="Quotes are one request per symbol-minute (slow)."),
    workers: int = typer.Option(6, min=1, max=16, help="Parallel quote requests."),
    merge_to: Path | None = typer.Option(None, help="Also write a merged bars/quotes.csv here."),
    strategy_symbols_only: bool = typer.Option(
        False, help="Quotes only for symbols a strategy trades (bars still cover the universe)."
    ),
) -> None:
    """Download many sessions of Alpaca (IEX) data. Resumable: re-run to continue after a stop."""
    from trading.data.alpaca_provider import ProviderError  # noqa: PLC0415
    from trading.data.collect import collect, merge_days, recent_sessions  # noqa: PLC0415

    settings = Settings()
    provider = _alpaca_provider(settings, workers=workers)
    if provider is None:
        typer.echo("no Alpaca key files (secrets/alpaca_key_id, secrets/alpaca_secret)", err=True)
        raise typer.Exit(1)
    config = load_config(settings.config_dir)
    symbols = [u.symbol for u in config.universe]
    now = SystemClock().now()
    calendar = XnysCalendar(
        now.date() - timedelta(days=days * 2 + 30), now.date() + timedelta(days=14)
    )
    sessions = recent_sessions(calendar, as_of=now, count=days)

    def log(message: str) -> None:
        typer.echo(f"{SystemClock().now():%Y-%m-%d %H:%M:%S}Z {message}", err=False)

    log(f"collecting {len(sessions)} sessions {sessions[0]}..{sessions[-1]} -> {out}")
    try:
        quote_symbols = None
        if strategy_symbols_only:
            traded = {
                s for cfg in config.strategies.values() for s in cfg.params.get("symbols", ())
            }
            quote_symbols = [s for s in symbols if s in traded]
        counts = collect(
            provider, symbols, calendar, sessions, out, quotes=quotes,
            quote_symbols=quote_symbols, log=log,
        )  # fmt: skip
    except ProviderError as exc:
        typer.echo(f"stopped: {exc} (re-run to resume)", err=True)
        raise typer.Exit(1) from None
    finally:
        provider.close()
    log(json.dumps(counts))
    if merge_to is not None:
        log("merged " + json.dumps(merge_days(out, sessions, merge_to)))


@app.command("alpaca-export")
def alpaca_export(
    start: str = typer.Option(..., help="Inclusive start, ISO-8601 with timezone."),
    end: str = typer.Option(..., help="Exclusive end, ISO-8601 with timezone."),
    out: Path = typer.Option(..., help="Directory to write bars.csv / quotes.csv."),
) -> None:
    """Download Alpaca (IEX) bars and quotes for the universe as replay/backtest CSV fixtures."""
    from trading.data.alpaca_provider import ProviderError  # noqa: PLC0415
    from trading.data.export import export_csv  # noqa: PLC0415

    settings = Settings()
    provider = _alpaca_provider(settings)
    if provider is None:
        typer.echo("no Alpaca key files (secrets/alpaca_key_id, secrets/alpaca_secret)", err=True)
        raise typer.Exit(1)
    symbols = [u.symbol for u in load_config(settings.config_dir).universe]
    try:
        counts = export_csv(
            provider, symbols, _parse_utc(start, "--start"), _parse_utc(end, "--end"), out
        )
    except ProviderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        provider.close()
    typer.echo(json.dumps(counts))


@app.command("ingest-alpaca")
def ingest_alpaca(
    start: str = typer.Option(..., help="Inclusive start, ISO-8601 with timezone."),
    end: str = typer.Option(..., help="Exclusive end, ISO-8601 with timezone."),
    quotes: bool = typer.Option(True, help="Quotes need one request per symbol-minute (slow)."),
) -> None:
    """Backfill Alpaca (IEX) bars and quotes for the configured universe into the database."""
    from trading.data.alpaca_provider import ProviderError  # noqa: PLC0415

    provider = _alpaca_provider(Settings())
    if provider is None:
        typer.echo("no Alpaca key files (secrets/alpaca_key_id, secrets/alpaca_secret)", err=True)
        raise typer.Exit(1)
    try:
        _ingest(provider, start, end, quotes=quotes)
    except ProviderError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        provider.close()


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
    from trading.execution.replay import ReplayError, replay_session  # noqa: PLC0415

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
def backtest(
    fixtures: Path = typer.Option(..., help="Directory with bars.csv / quotes.csv."),
    start: str = typer.Option(..., help="First date, YYYY-MM-DD."),
    end: str = typer.Option(..., help="Last date, YYYY-MM-DD."),
    minutes_per_day: int | None = typer.Option(None, min=1, help="Shorten sessions (research)."),
    reset: bool = typer.Option(False, "--reset", help="Wipe the scratch database first."),
) -> None:
    """Backtest the strategies over several sessions (use a scratch database)."""
    from trading.quant.backtest import BacktestError, run_backtest  # noqa: PLC0415

    settings = Settings()
    config = load_config(settings.config_dir)
    engine = make_engine(settings.resolved_database_url())
    try:
        result = run_backtest(
            make_session_factory(engine),
            fixtures_dir=fixtures,
            start=date.fromisoformat(start),
            end=date.fromisoformat(end),
            config=config,
            minutes_per_day=minutes_per_day,
            reset=reset,
        )
    except BacktestError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    finally:
        engine.dispose()
    typer.echo(
        json.dumps(
            {
                "sessions": [d.isoformat() for d in result.sessions],
                "result_hash": result.result_hash,
                "final_equity": str(result.final_equity),
                "net_pnl": str(result.account.net_pnl),
                "max_drawdown_pct": str(result.account.max_drawdown_pct),
                "benchmark_return_pct": str(
                    result.account.benchmark.return_pct if result.account.benchmark else None
                ),
                "strategy_net_pnl": {k: str(v.net_pnl) for k, v in result.strategies.items()},
            }
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
