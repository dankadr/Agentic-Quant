"""Multi-day backtest through the production tick path.

One persistent paper account runs every session in the range against CSV fixtures and a
``ManualClock``. It takes the executor lease and wipes the database between runs, so it only ever
runs on a scratch database: anything holding an account that is not a replay/backtest account is
refused before a single row is touched.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.core.config import AppConfig
from trading.core.errors import TradingError
from trading.data.calendar import XnysCalendar
from trading.data.replay_provider import CsvReplayProvider
from trading.db.models.portfolio import Account
from trading.db.seed import seed_defaults
from trading.execution.replay import drive_session, summarize_run
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease, release_lease
from trading.quant.performance import PerformanceReport, load_report
from trading.quant.proposals import SCRATCH_ACCOUNT_PREFIXES
from trading.quant.research import Evaluation, Evaluator
from trading.services.bootstrap import build_runtime

_ONE_MINUTE = timedelta(minutes=1)


class BacktestError(TradingError):
    """The backtest cannot run (not a scratch database, empty range, lease held)."""


@dataclass(frozen=True)
class BacktestResult:
    sessions: list[date]
    daily_returns: tuple[Decimal, ...]
    account: PerformanceReport
    strategies: dict[str, PerformanceReport]
    strategy_daily_returns: dict[str, tuple[Decimal, ...]]
    final_equity: Decimal
    result_hash: str


def ensure_scratch(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as session:
        foreign = [
            n
            for n in session.scalars(select(Account.name))
            if not n.startswith(SCRATCH_ACCOUNT_PREFIXES)
        ]
    if foreign:
        raise BacktestError(
            f"this database holds non-backtest accounts {foreign}; use a scratch database"
        )


def reset_scratch(session_factory: sessionmaker[Session], *, now: datetime) -> None:
    """Empty every application table and restore the fail-closed seed. Scratch databases only."""
    ensure_scratch(session_factory)
    with session_factory.begin() as session:
        tables = session.execute(
            text(
                "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) "
                "FROM pg_tables WHERE schemaname IN ('public', 'paper_venue') "
                "AND tablename <> 'alembic_version'"
            )
        ).scalars()
        names = ", ".join(tables)
        if names:
            session.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        seed_defaults(session, now)


def run_backtest(
    session_factory: sessionmaker[Session],
    *,
    fixtures_dir: Path,
    start: date,
    end: date,
    config: AppConfig,
    account_name: str = "backtest",
    minutes_per_day: int | None = None,
    reset: bool = True,
) -> BacktestResult:
    """Replay every session in ``[start, end]``. ``minutes_per_day`` only shortens sessions."""
    if not account_name.startswith(SCRATCH_ACCOUNT_PREFIXES):
        raise BacktestError(
            f"account name {account_name!r} must start with {SCRATCH_ACCOUNT_PREFIXES}"
        )
    calendar = XnysCalendar(start - timedelta(days=10), end + timedelta(days=10))
    infos = []
    cursor = start
    while cursor <= end:
        info = calendar.session_on(cursor)
        if info is not None:
            infos.append(info)
        cursor += timedelta(days=1)
    if not infos:
        raise BacktestError(f"no trading sessions between {start} and {end}")
    if reset:
        reset_scratch(session_factory, now=infos[0].open_at - timedelta(minutes=30))
    else:
        ensure_scratch(session_factory)

    provider = CsvReplayProvider(fixtures_dir)
    for info in infos:
        clock = ManualClock(info.open_at - timedelta(minutes=30))
        with session_factory.begin() as session:
            lease = acquire_lease(
                session,
                name=EXECUTOR_LEASE,
                holder="backtest",
                now=clock.now(),
                ttl=timedelta(days=2),
            )
        if lease is None:
            raise BacktestError("the executor lease is held by another process; use a scratch DB")
        try:
            runtime = build_runtime(
                session_factory=session_factory,
                config=config,
                clock=clock,
                lease=lease,
                calendar=calendar,
                provider=provider,
                provider_name=provider.name,
                account_name=account_name,
                intents=None,  # strategies
            )
            drive_session(
                runtime.processor, session_factory, clock, info, max_minutes=minutes_per_day
            )
        finally:
            with session_factory.begin() as session:
                release_lease(session, lease)

    with session_factory() as session:
        account_id = session.scalar(select(Account.id).where(Account.name == account_name))
        if account_id is None:  # pragma: no cover - build_runtime creates it
            raise BacktestError("backtest account missing")
        first, last = infos[0].open_at, infos[-1].close_at + timedelta(hours=1)
        account = load_report(
            session, account_id=account_id, start=first, end=last, provider=provider.name
        )
        strategies = {
            sid: load_report(
                session,
                account_id=account_id,
                start=first,
                end=last,
                strategy_id=sid,
                provider=provider.name,
            )
            for sid in session.scalars(text("SELECT id FROM strategies ORDER BY id")).all()
        }
        edges = session.execute(
            text(
                "SELECT kind, equity FROM equity_snapshots WHERE account_id = :a"
                " AND kind IN ('SESSION_START', 'SESSION_END') ORDER BY ts, kind DESC"
            ),
            {"a": account_id},
        ).all()
    returns = tuple(
        (edges[i + 1].equity - edges[i].equity) / edges[i].equity
        for i in range(0, len(edges) - 1, 2)
        if edges[i].kind == "SESSION_START" and edges[i].equity > 0
    )
    start_equity = edges[0].equity if edges else Decimal(0)
    per_strategy = {
        sid: _daily_pnl_returns(rep.equity_curve, [i.close_at for i in infos], start_equity)
        for sid, rep in strategies.items()
    }
    digest = summarize_run(session_factory)
    return BacktestResult(
        sessions=[i.session_date for i in infos],
        daily_returns=returns,
        account=account,
        strategies=strategies,
        strategy_daily_returns=per_strategy,
        final_equity=digest.final_equity,
        result_hash=digest.result_hash,
    )


def _daily_pnl_returns(
    curve: Sequence[tuple[datetime, Decimal]], closes: Sequence[datetime], base: Decimal
) -> tuple[Decimal, ...]:
    """Per-session change of a cumulative-P&L curve as a fraction of starting equity."""
    if base <= 0:
        return ()
    out: list[Decimal] = []
    previous = Decimal(0)
    for close in closes:
        level = previous
        for ts, value in curve:
            if ts <= close + timedelta(minutes=5):
                level = value
        out.append((level - previous) / base)
        previous = level
    return tuple(out)


def backtest_evaluator(
    session_factory: sessionmaker[Session],
    *,
    fixtures_dir: Path,
    config: AppConfig,
    strategy_id: str,
    minutes_per_day: int | None = None,
) -> Evaluator:
    """Adapt ``run_backtest`` to the research ``Evaluator`` protocol.

    ``params`` override that strategy's configured parameters for one fresh run (the database is
    reset first). Dates are the contiguous sessions to replay.
    """

    def evaluate(dates: Sequence[date], params: Mapping[str, Any]) -> Evaluation:
        ordered = sorted(dates)
        base = config.strategies[strategy_id]
        merged = {**base.params, **params}
        tuned = config.model_copy(
            update={
                "strategies": {
                    **config.strategies,
                    strategy_id: base.model_copy(update={"params": merged}),
                }
            }
        )
        result = run_backtest(
            session_factory,
            fixtures_dir=fixtures_dir,
            start=ordered[0],
            end=ordered[-1],
            config=tuned,
            minutes_per_day=minutes_per_day,
        )
        report = result.strategies[strategy_id]
        return Evaluation(
            params=dict(params),
            dates=tuple(result.sessions),
            net_pnl=report.net_pnl,
            daily_returns=result.strategy_daily_returns[strategy_id],
            trades=report.trades.trades,
            expectancy=report.trades.expectancy or Decimal(0),
            max_drawdown_pct=report.max_drawdown_pct,
        )

    return evaluate
