"""Deterministic offline replay of one recorded session.

Runs the exact production path (ingest -> snapshot -> paper broker -> risk -> execution ->
reconciliation) against a ``ManualClock`` and CSV fixtures. Run it on a scratch database: it
takes the executor lease, so it cannot run next to a live worker.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from trading.control.state import TradingState, get_system_state, set_trading_state
from trading.core.clock import ManualClock
from trading.core.config import AppConfig
from trading.core.errors import TradingError
from trading.core.hashing import content_hash
from trading.data.calendar import SessionInfo, XnysCalendar
from trading.data.replay_provider import CsvReplayProvider
from trading.db.models.portfolio import Account, EquitySnapshot
from trading.execution.intents import ScriptedIntentSource
from trading.execution.tick import PaperTickProcessor
from trading.jobs.lease import EXECUTOR_LEASE, acquire_lease, release_lease
from trading.services.bootstrap import build_runtime

_ONE_MINUTE = timedelta(minutes=1)


class ReplayError(TradingError):
    """The replay cannot run (no such session, lease held, nothing to resume)."""


@dataclass(frozen=True)
class ReplayResult:
    result_hash: str
    orders: int
    fills: int
    final_equity: Decimal
    decisions: int


def replay_session(
    session_factory: sessionmaker[Session],
    *,
    fixtures_dir: Path,
    session_date: date,
    script: Path | None,
    config: AppConfig,
    stop_after: datetime | None = None,
    resume: bool = False,
) -> ReplayResult:
    calendar = XnysCalendar(session_date - timedelta(days=10), session_date + timedelta(days=10))
    info = calendar.session_on(session_date)
    if info is None:
        raise ReplayError(f"{session_date} is not a trading session")
    clock = ManualClock(info.open_at - timedelta(minutes=30))
    account_name = f"replay-{session_date.isoformat()}"
    with session_factory() as session:
        foreign = [n for n in session.scalars(select(Account.name)) if not n.startswith("replay-")]
    if foreign:
        raise ReplayError(
            f"this database holds non-replay accounts {foreign}; replay only on a scratch database"
        )
    with session_factory.begin() as session:
        lease = acquire_lease(
            session, name=EXECUTOR_LEASE, holder="replay", now=clock.now(), ttl=timedelta(days=2)
        )
    if lease is None:
        raise ReplayError("the executor lease is held by another process; replay on a scratch DB")

    try:
        provider = CsvReplayProvider(fixtures_dir)
        runtime = build_runtime(
            session_factory=session_factory,
            config=config,
            clock=clock,
            lease=lease,
            calendar=calendar,
            provider=provider,
            provider_name=provider.name,
            account_name=account_name,
            intents=ScriptedIntentSource.from_yaml(script) if script else None,  # None = strategies
        )
        first_minute = info.open_at + _ONE_MINUTE
        if resume:
            with session_factory() as session:
                last = session.scalar(
                    select(func.max(EquitySnapshot.ts)).where(
                        EquitySnapshot.account_id == runtime.account_id,
                        EquitySnapshot.kind == "INTRADAY",
                    )
                )
            if last is None:
                raise ReplayError("nothing to resume: no processed ticks")
            first_minute = last.replace(second=0, microsecond=0) + _ONE_MINUTE
            clock.set(last)

        interrupted = drive_session(
            runtime.processor,
            session_factory,
            clock,
            info,
            first_minute=first_minute,
            stop_after=stop_after,
        )
        if not interrupted:
            with session_factory.begin() as session:
                release_lease(session, lease)
    except BaseException:
        with session_factory.begin() as session:
            release_lease(session, lease)  # a crashed replay must not lock out the real worker
        raise
    return _summarize(session_factory)


def drive_session(
    processor: PaperTickProcessor,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    info: SessionInfo,
    *,
    first_minute: datetime | None = None,
    stop_after: datetime | None = None,
    max_minutes: int | None = None,
) -> bool:
    """Run one session through the production tick path. Returns True if it stopped early.

    ``stop_after`` simulates a crash (no end-of-session). ``max_minutes`` only shortens a session
    for fast research runs; the session is still closed normally.
    """
    session_date = info.session_date
    processor.start_session(session_date)
    with session_factory.begin() as session:
        state = get_system_state(session)
        if state.trading_state != TradingState.PAPER_RUNNING:
            set_trading_state(
                session,
                target=TradingState.PAPER_RUNNING,
                expected_version=state.version,
                actor="replay",
                reason="replay",
                request_id=None,
                now=clock.now(),
            )
    minute = first_minute or info.open_at + _ONE_MINUTE
    ticks = 0
    while minute <= info.close_at:
        if stop_after is not None and minute > stop_after:
            return True
        if max_minutes is not None and ticks >= max_minutes:
            break
        clock.set(minute + timedelta(seconds=5))
        processor.run_tick(minute)
        minute += _ONE_MINUTE
        ticks += 1
    clock.set(info.close_at + timedelta(minutes=5))
    processor.end_session(session_date)
    return False


def _summarize(session_factory: sessionmaker[Session]) -> ReplayResult:
    """Hash only deterministic facts: never UUIDs, row ids or wall-clock times."""
    with session_factory() as session:
        orders = [
            [r[0], r[1], r[2], r[3], r[4]]
            for r in session.execute(
                text(
                    "SELECT client_order_id, status, qty, filled_qty, avg_fill_price"
                    " FROM orders ORDER BY client_order_id"
                )
            )
        ]
        fills = [
            [r[0], r[1], r[2], r[3]]
            for r in session.execute(
                text("SELECT execution_id, qty, price, fees FROM fills ORDER BY execution_id")
            )
        ]
        ledger = [
            [r[0], r[1]]
            for r in session.execute(
                text(
                    "SELECT source_type, amount FROM cash_ledger"
                    " ORDER BY ts, source_type, amount, source_id"
                )
            )
        ]
        equity = session.scalar(
            select(EquitySnapshot.equity).order_by(EquitySnapshot.ts.desc()).limit(1)
        ) or Decimal(0)
        decisions = session.scalar(text("SELECT count(*) FROM risk_decisions")) or 0
    digest = content_hash({"orders": orders, "fills": fills, "ledger": ledger, "equity": equity})
    return ReplayResult(
        result_hash=digest,
        orders=len(orders),
        fills=len(fills),
        final_equity=equity,
        decisions=int(decisions),
    )
