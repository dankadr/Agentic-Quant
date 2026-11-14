"""Serialized scratch-only workflows. Never claim execution or intraday model jobs."""

import csv
import hashlib
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.macro import JsonLlm
from trading.agents.runs import record_skip
from trading.agents.technical import run_technical_agent
from trading.core.clock import Clock
from trading.core.config import AppConfig, Settings
from trading.core.hashing import content_hash
from trading.data.calendar import MarketCalendar
from trading.data.collect import collect, merge_days, merged_dates, recent_sessions
from trading.data.export import write_bars, write_quotes
from trading.data.models import Bar, Quote
from trading.data.provider import MarketDataProvider
from trading.db.engine import make_engine, make_session_factory
from trading.db.models.market import Instrument, MarketBar, MarketQuote
from trading.db.models.research import AgentRun, Strategy
from trading.jobs.kinds import HEAVY_KINDS
from trading.jobs.queue import Job
from trading.jobs.runner import Handler
from trading.quant.backtest import BacktestResult, backtest_full_run, ensure_scratch, run_backtest
from trading.quant.research import record_experiment
from trading.quant.research_config import load_research_spaces
from trading.quant.research_runner import disk_cached, run_research
from trading.services.research import run_research_worker

_SCRATCH_MARKERS = ("scratch", "test", "replay", "backtest")
_LOCK = 16844510


def validate_scratch_url(scratch: str | None, live: str) -> str:
    if not scratch or not any(
        m in (make_url(scratch).database or "").lower() for m in _SCRATCH_MARKERS
    ):
        raise ValueError("a separate named scratch database is required")
    a, b = make_url(scratch), make_url(live)
    if (a.host, a.port or 5432, a.database) == (b.host, b.port or 5432, b.database):
        raise ValueError("scratch and registry refer to the same database")
    return scratch


def fixture_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("bars.csv", "quotes.csv"):
        digest.update(name.encode())
        with (directory / name).open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def _entry_config(config: AppConfig, limit: int) -> AppConfig:
    strategies = dict(config.strategies)
    for sid in ("orb_v1", "vwap_v1"):
        current = strategies[sid]
        params = {
            **current.params,
            "max_entries_per_symbol_per_day": limit,
            "reentry_cooldown_minutes": current.params.get("reentry_cooldown_minutes", 30),
        }
        strategies[sid] = current.model_copy(update={"params": params})
    return config.model_copy(update={"strategies": strategies})


def compare_entries(
    scratch: sessionmaker[Session],
    *,
    fixtures: Path,
    config: AppConfig,
    day: date,
    runner: Callable[..., BacktestResult] = run_backtest,
    prepare: Callable[[sessionmaker[Session]], None] | None = None,
) -> dict[str, Any]:
    """Paired fresh accounts, identical input, only the entry limit differs."""
    result: dict[str, Any] = {}
    for label, limit in (("baseline", 1), ("candidate", 2)):
        kwargs: dict[str, Any] = {}
        if prepare is not None:
            kwargs["prepare"] = prepare
        run = runner(
            scratch,
            fixtures_dir=fixtures,
            start=day,
            end=day,
            config=_entry_config(config, limit),
            account_name=f"backtest-{label}",
            reset=True,
            **kwargs,
        )
        report = run.account
        result[label] = {
            "net_pnl": str(report.net_pnl),
            "trades": report.trades.trades,
            "fees": str(report.costs.fees),
            "slippage_estimate": str(report.costs.slippage),
            "max_drawdown_fraction": str(report.max_drawdown_pct),
            "result_hash": run.result_hash,
        }
    return result


def effective_strategy_config(
    sf: sessionmaker[Session], config: AppConfig
) -> tuple[AppConfig, dict[str, str]]:
    """Research is bound to approved stored parameters, never stale YAML defaults."""
    strategies = dict(config.strategies)
    versions = {}
    with sf() as session:
        for row in session.scalars(select(Strategy)):
            if row.id in strategies:
                strategies[row.id] = strategies[row.id].model_copy(
                    update={"params": dict(row.params_json)}
                )
                versions[row.id] = row.version
    return config.model_copy(update={"strategies": strategies}), versions


@contextmanager
def scratch_lock(sf: sessionmaker[Session]):  # type: ignore[no-untyped-def]
    # Session-level lock survives resets while this connection stays checked out.
    with sf() as session:
        locked = session.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK}
        ).scalar_one()
        if not locked:
            raise ValueError("scratch_busy")
        try:
            ensure_scratch(sf)
            yield
        finally:
            session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK})


def _record(
    sf: sessionmaker[Session],
    *,
    kind: str,
    now: datetime,
    status: str,
    metrics: Mapping[str, Any],
    params: Mapping[str, Any],
    day: date | None = None,
) -> None:
    with sf.begin() as session:
        record_experiment(
            session,
            kind=kind,
            strategy_id=None,
            hypothesis="paired paper entry experiment",
            params=params,
            data_range={"date": day.isoformat() if day else None},
            metrics=metrics,
            status=status,
            now=now,
        )


def export_recorded(
    sf: sessionmaker[Session], directory: Path, *, provider: str, info: Any, as_of: datetime
) -> int:
    """Export recorded records only, never download hindsight data for the paired day."""
    directory.mkdir(parents=True, exist_ok=True)
    with sf() as session:
        availability = {}
        bars = [
            Bar(
                symbol=symbol,
                interval="1m",
                ts=b.ts,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                provider=provider,
                source_seq=b.source_seq,
            )
            for b, symbol in session.execute(
                select(MarketBar, Instrument.symbol)
                .join(Instrument, Instrument.id == MarketBar.instrument_id)
                .where(
                    MarketBar.provider == provider,
                    MarketBar.ts >= info.open_at,
                    MarketBar.ts < info.close_at,
                    MarketBar.ingested_at <= as_of,
                )
            )
        ]
        quotes = [
            Quote(
                symbol=symbol,
                ts=q.ts,
                bid=q.bid,
                ask=q.ask,
                bid_size=q.bid_size,
                ask_size=q.ask_size,
                provider=provider,
            )
            for q, symbol in session.execute(
                select(MarketQuote, Instrument.symbol)
                .join(Instrument, Instrument.id == MarketQuote.instrument_id)
                .where(
                    MarketQuote.provider == provider,
                    MarketQuote.ts >= info.open_at,
                    MarketQuote.ts <= info.close_at,
                    MarketQuote.ingested_at <= as_of,
                )
            )
        ]
    with sf() as session:
        for model, name in ((MarketBar, "bars.csv"), (MarketQuote, "quotes.csv")):
            availability[name] = {
                (symbol, cast(MarketBar | MarketQuote, row).ts.isoformat()): (
                    cast(MarketBar | MarketQuote, row).ingested_at.isoformat(),
                    "|".join(cast(MarketBar | MarketQuote, row).quality_flags),
                )
                for row, symbol in session.execute(
                    select(model, Instrument.symbol)
                    .join(Instrument, Instrument.id == model.instrument_id)
                    .where(
                        model.provider == provider,
                        model.ts >= info.open_at,
                        model.ts <= info.close_at,
                    )
                )
            }
    write_bars(directory / "bars.csv", bars)
    write_quotes(directory / "quotes.csv", quotes)
    for name, lookup in availability.items():
        path = directory / name
        with path.open() as handle:
            reader = csv.DictReader(handle)
            fields = [*(reader.fieldnames or []), "available_at", "quality_flags"]
            rows = list(reader)
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                available, flags = lookup[
                    (row["symbol"], datetime.fromisoformat(row["ts"]).isoformat())
                ]
                writer.writerow({**row, "available_at": available, "quality_flags": flags})
    return len(quotes) if bars else 0


def _macro_prepare(
    sf: sessionmaker[Session], end: datetime
) -> Callable[[sessionmaker[Session]], None]:
    with sf() as session:
        runs = list(
            session.scalars(
                select(AgentRun).where(
                    AgentRun.agent_name == "macro",
                    AgentRun.status == "COMPLETED",
                    AgentRun.available_at.is_not(None),
                    AgentRun.available_at <= end,
                )
            )
        )
        session.expunge_all()

    def prepare(scratch: sessionmaker[Session]) -> None:
        with scratch.begin() as session:
            for run in runs:
                session.add(
                    AgentRun(
                        id=uuid4(),
                        agent_name="macro",
                        model=run.model,
                        prompt_version=run.prompt_version,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                        available_at=run.available_at,
                        result_json=run.result_json,
                        status=run.status,
                        attempt=run.attempt,
                    )
                )

    return prepare


def retrospective(rows: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if Decimal(r.get("starting_equity", "0")) > 0]
    differences = [
        (Decimal(r["candidate"]["net_pnl"]) - Decimal(r["baseline"]["net_pnl"]))
        / Decimal(r["starting_equity"])
        for r in valid
    ]
    return {
        "sessions": len(valid),
        "mean_return_difference": str(sum(differences, Decimal(0)) / len(valid)) if valid else None,
        "extra_trades": sum(r["candidate"]["trades"] - r["baseline"]["trades"] for r in valid),
        "limitations": ["descriptive paired replay results, not evidence of future profitability"],
    }


def comparison_job(
    sf: sessionmaker[Session],
    scratch: sessionmaker[Session],
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    root: Path,
    day: date,
    now: datetime,
    versions: dict[str, str] | None = None,
) -> None:
    info = calendar.session_on(day)
    if info is None or now < info.close_at:
        raise ValueError("comparison requires a finished session")
    if versions is None:
        config, versions = effective_strategy_config(sf, config)
    with sf() as session:
        # Same starting funding for both arms; deposits are never investment returns.
        start_equity = session.execute(
            text(
                "SELECT equity FROM equity_snapshots e JOIN accounts a"
                " ON a.id=e.account_id WHERE a.name='paper-primary' AND kind='SESSION_START'"
                " AND ts>=:s AND ts<=:e ORDER BY ts LIMIT 1"
            ),
            {"s": info.open_at - timedelta(hours=1), "e": info.close_at},
        ).scalar()
    if start_equity is not None:
        config = config.model_copy(
            update={"system": config.system.model_copy(update={"starting_cash_usd": start_equity})}
        )
    directory = root / "comparisons" / day.isoformat()
    if not export_recorded(sf, directory, provider=provider_name, info=info, as_of=now):
        _record(
            sf,
            kind="PAPER_COMPARISON",
            now=now,
            status="SKIPPED",
            metrics={"skip_reason": "no_recorded_data"},
            params={},
            day=day,
        )
        return
    fingerprint = {
        "data": fixture_fingerprint(directory),
        "config": content_hash(config.model_dump()),
        "versions": versions,
        "day": day.isoformat(),
    }
    with sf() as session:
        exists = session.execute(
            text(
                "SELECT 1 FROM experiments WHERE kind='PAPER_COMPARISON'"
                " AND status='COMPLETED' AND params_json->>'fingerprint'=:f"
            ),
            {"f": content_hash(fingerprint)},
        ).first()
    if exists:
        return
    metrics = compare_entries(
        scratch,
        fixtures=directory,
        config=config,
        day=day,
        prepare=_macro_prepare(sf, info.close_at),
    )
    metrics["limitations"] = [
        "paired fresh simulated accounts; not a reconstruction of live fills",
        "recorded ingestion availability and blocking quality flags are honored in replay",
        "no overnight holdings; unchanged macro availability is replayed in both arms",
    ]
    with sf() as session:
        days = session.execute(
            text(
                "SELECT count(DISTINCT data_range_json->>'date') FROM experiments"
                " WHERE kind='PAPER_COMPARISON' AND status='COMPLETED'"
            )
        ).scalar_one()
    metrics["starting_equity"] = str(config.system.starting_cash_usd)
    metrics["review_due"] = days + 1 >= 10
    if metrics["review_due"]:
        with sf() as session:
            prior = list(
                session.execute(
                    text(
                        "SELECT DISTINCT ON (data_range_json->>'date') "
                        "data_range_json->>'date' AS day, metrics_json"
                        " FROM experiments WHERE kind='PAPER_COMPARISON' AND status='COMPLETED'"
                        " AND data_range_json->>'date' < :day "
                        "ORDER BY data_range_json->>'date' DESC, created_at DESC LIMIT 9"
                    ),
                    {"day": day.isoformat()},
                ).mappings()
            )
        metrics["retrospective"] = retrospective([r["metrics_json"] for r in prior] + [metrics])
    _record(
        sf,
        kind="PAPER_COMPARISON",
        now=now,
        status="COMPLETED",
        metrics=metrics,
        params={"fingerprint": content_hash(fingerprint), **fingerprint},
        day=day,
    )


def research_data_ready(directory: Path, days: list[date], symbols: list[str]) -> bool:
    for filename, minimum in (("bars.csv", 35), ("quotes.csv", 1)):
        counts: dict[tuple[str, str], int] = {}
        with (directory / filename).open() as handle:
            for row in csv.DictReader(handle):
                key = (row["symbol"], row["ts"][:10])
                counts[key] = counts.get(key, 0) + 1
        if any(
            counts.get((symbol, day.isoformat()), 0) < minimum for symbol in symbols for day in days
        ):
            return False
    return True


def weekly_research(
    sf: sessionmaker[Session],
    scratch: sessionmaker[Session],
    *,
    settings: Settings,
    config: AppConfig,
    calendar: MarketCalendar,
    provider: MarketDataProvider | None,
    llm: JsonLlm | None,
    root: Path,
    clock: Clock,
) -> None:
    now = clock.now()

    def skip(reason: str) -> None:
        record_skip(
            sf,
            agent_name="technical",
            model=config.agents.llm.model,
            prompt_version="technical-v1",
            reason=reason,
            now=clock.now(),
        )

    if not config.agents.technical.enabled:
        return
    if llm is None or provider is None:
        skip("no_llm_key" if llm is None else "no_market_data_provider")
        return
    days = recent_sessions(calendar, as_of=now, count=30)
    if len(days) != 30:
        skip("insufficient_sessions")
        return
    config, versions = effective_strategy_config(sf, config)
    collection = root / "sessions"
    merged = root / "merged"
    symbols = [u.symbol for u in config.universe]
    collect(provider, symbols, calendar, days, collection)
    merge_days(collection, days, merged)
    if merged_dates(merged) != days:
        skip("incomplete_collection")
        return
    quote_symbols = sorted(
        {
            symbol
            for sid in ("orb_v1", "vwap_v1")
            for symbol in config.strategies[sid].params["symbols"]
        }
    )
    if not research_data_ready(merged, days, quote_symbols):
        skip("insufficient_bar_or_quote_coverage")
        return
    spaces = load_research_spaces(settings.config_dir)
    for sid in ("orb_v1", "vwap_v1"):
        if sid not in spaces or sid not in versions:
            skip(f"missing_grid_or_strategy:{sid}")
            continue
        space = spaces[sid]
        base = {k: config.strategies[sid].params[k] for k in space}
        neighbors = {k: [v for v in vals if v != base[k]] for k, vals in space.items()}
        fp = {
            "data": fixture_fingerprint(merged),
            "config": content_hash(config.model_dump()),
            "strategy": sid,
            "version": versions[sid],
            "revision": settings.release_revision,
        }
        with sf() as session:
            already = session.execute(
                text(
                    "SELECT 1 FROM experiments WHERE kind='RESEARCH_BATCH'"
                    " AND strategy_id=:s AND params_json->>'fingerprint'=:f AND status='COMPLETED'"
                ),
                {"s": sid, "f": content_hash(fp)},
            ).first()
        if already:
            continue
        run_research(
            sf,
            disk_cached(
                backtest_full_run(
                    scratch, fixtures_dir=merged, config=config, strategy_id=sid, dates=days
                ),
                root / "cache",
                fp,
            ),
            strategy_id=sid,
            dates=days,
            base_params=base,
            space=space,
            neighbors=neighbors,
            train=10,
            test=5,
            holdout=5,
            base_equity=config.system.starting_cash_usd,
            now=clock.now(),
            evaluate_holdout=False,
            strategy_version=versions[sid],
        )
        run_technical_agent(sf, llm, config=config, strategy_id=sid, now=clock.now())
        with sf.begin() as session:
            record_experiment(
                session,
                kind="RESEARCH_BATCH",
                strategy_id=sid,
                hypothesis="weekly parameter research with untouched holdout",
                params={"fingerprint": content_hash(fp)},
                data_range={"first": str(days[0]), "last": str(days[-1])},
                metrics=fp,
                status="COMPLETED",
                now=clock.now(),
            )


def build_heavy_handlers(
    *,
    sf: sessionmaker[Session],
    settings: Settings,
    config: AppConfig,
    calendar: MarketCalendar,
    clock: Clock,
    provider: MarketDataProvider | None,
    provider_name: str,
    llm: JsonLlm | None,
    root: Path,
) -> dict[str, Handler]:
    def run(job: Job) -> None:
        try:
            url = validate_scratch_url(
                settings.scratch_database_url, settings.resolved_database_url()
            )
        except ValueError:
            if job.kind == "quant_research":
                record_skip(
                    sf,
                    agent_name="technical",
                    model=config.agents.llm.model,
                    prompt_version="technical-v1",
                    reason="scratch_not_configured",
                    now=clock.now(),
                )
            else:
                _record(
                    sf,
                    kind="PAPER_COMPARISON",
                    now=clock.now(),
                    status="SKIPPED",
                    metrics={"skip_reason": "scratch_not_configured"},
                    params={},
                    day=date.fromisoformat(str(job.payload["session_date"])),
                )
            return
        engine = make_engine(url)
        try:
            scratch = make_session_factory(engine)
            with scratch_lock(scratch):
                if job.kind == "quant_research":
                    weekly_research(
                        sf,
                        scratch,
                        settings=settings,
                        config=config,
                        calendar=calendar,
                        provider=provider,
                        llm=llm,
                        root=root,
                        clock=clock,
                    )
                else:
                    snapshot = job.payload.get("session_config")
                    if not isinstance(snapshot, dict):
                        _record(
                            sf,
                            kind="PAPER_COMPARISON",
                            now=clock.now(),
                            status="SKIPPED",
                            metrics={"skip_reason": "missing_session_configuration"},
                            params={},
                            day=date.fromisoformat(str(job.payload["session_date"])),
                        )
                        return
                    comparison_job(
                        sf,
                        scratch,
                        config=AppConfig.model_validate(snapshot),
                        versions=dict(job.payload.get("strategy_versions", {})),
                        calendar=calendar,
                        provider_name=provider_name,
                        root=root,
                        day=date.fromisoformat(str(job.payload["session_date"])),
                        now=clock.now(),
                    )
        finally:
            engine.dispose()

    return {kind: run for kind in HEAVY_KINDS}


def run_heavy_worker(
    *,
    sf: sessionmaker[Session],
    clock: Clock,
    worker_id: str,
    handlers: dict[str, Handler],
    stop: threading.Event,
    config_version: str | None = None,
) -> int:
    return run_research_worker(
        session_factory=sf,
        clock=clock,
        worker_id=worker_id,
        handlers=handlers,
        stop=stop,
        config_version=config_version,
        kinds=HEAVY_KINDS,
    )
