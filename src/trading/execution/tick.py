"""The paper tick processor: one deterministic pass per market minute.

Order of operations per tick: ingest -> point-in-time snapshot -> simulated broker tick -> sync
fills -> value + halts -> proposals (risk-checked, then executed). Halts are evaluated BEFORE any
proposal, so a halt re-derived after a reconciliation reset is always in force first.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.paper.broker import PaperBroker
from trading.control.state import RiskState, get_system_state, set_risk_state
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.enums import TimeInForce
from trading.core.errors import TradingError
from trading.data.calendar import MarketCalendar
from trading.data.ingest import ingest_bars, ingest_quotes
from trading.data.instruments import InstrumentInfo
from trading.data.provider import MarketDataProvider
from trading.data.snapshot import MarketSnapshot, build_snapshot
from trading.db.models.market import Instrument, MarketBar
from trading.db.models.portfolio import Account, Position, TradingSession
from trading.db.models.trading import Order, RiskDecisionRow
from trading.execution.intents import IntentSource, ProposalRequest
from trading.execution.manager import ExecutionManager
from trading.execution.reconcile import ReconciliationReport, reconcile_account
from trading.jobs.lease import Lease, LeaseLost, assert_fence
from trading.monitoring.audit import record_audit
from trading.monitoring.logging import get_logger
from trading.portfolio.valuation import (
    MissingMark,
    PortfolioState,
    record_equity_snapshot,
    value_portfolio,
)
from trading.quant.proposals import SCRATCH_ACCOUNT_PREFIXES, apply_approved
from trading.risk.halts import evaluate_halts, start_session
from trading.risk.models import ProposedOrder, RiskEngine
from trading.risk.service import build_account_view, evaluate_and_record

_log = get_logger(__name__)
_ONE_MINUTE = timedelta(minutes=1)
_STUCK_STATUSES = ("UNKNOWN", "SUBMITTING", "CREATED", "RISK_APPROVED")


@dataclass(frozen=True)
class TickResult:
    minute: datetime
    snapshot_id: UUID | None
    fills_applied: int
    decisions: int
    orders_submitted: int
    risk_state: RiskState


class PaperTickProcessor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        account_id: UUID,
        broker: PaperBroker,
        manager: ExecutionManager,
        engine: RiskEngine,
        provider: MarketDataProvider | None,
        provider_name: str,
        intents: IntentSource,
        instruments: dict[str, InstrumentInfo],
        calendar: MarketCalendar,
        config: AppConfig,
        clock: Clock,
        lease: Lease,
    ) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._broker = broker
        self._manager = manager
        self._engine = engine
        self.provider = provider
        self._provider_name = provider_name
        self._intents = intents
        self._instruments = instruments
        self._calendar = calendar
        self._config = config
        self._clock = clock
        self._lease = lease
        self._symbols = [u.symbol for u in config.universe]

    # ---- helpers -------------------------------------------------------------------------

    def _fence(self, session: Session) -> datetime:
        now = self._clock.now()
        assert_fence(session, self._lease, now=now)
        return now

    def _value(
        self,
        session: Session,
        *,
        marks: dict[str, Decimal],
        asks: dict[str, Decimal],
        now: datetime,
        session_date: date,
    ) -> PortfolioState:
        return value_portfolio(
            session,
            self._account_id,
            marks=marks,
            ask_by_symbol=asks,
            as_of=now,
            session_date=session_date,
            cfg=self._config.paper_broker,
        )

    def _value_or_fail_closed(
        self,
        session: Session,
        *,
        marks: dict[str, Decimal],
        asks: dict[str, Decimal],
        now: datetime,
        session_date: date,
    ) -> PortfolioState | None:
        """Value the account; if a price is missing, mark the risk state UNKNOWN and return None."""
        try:
            return self._value(session, marks=marks, asks=asks, now=now, session_date=session_date)
        except MissingMark:
            set_risk_state(
                session,
                risk_state=RiskState.UNKNOWN,
                reason="missing_mark",
                actor="system",
                now=now,
            )
            return None

    def _db_marks(self, session: Session, as_of: datetime) -> dict[str, Decimal]:
        """Latest known close for every held symbol (used when no snapshot is at hand)."""
        marks: dict[str, Decimal] = {}
        held = session.execute(
            select(Instrument.id, Instrument.symbol)
            .join(Position, Position.instrument_id == Instrument.id)
            .where(Position.account_id == self._account_id, Position.qty > 0)
        ).all()
        for instrument_id, symbol in held:
            close = session.scalar(
                select(MarketBar.close)
                .where(MarketBar.instrument_id == instrument_id, MarketBar.ts <= as_of)
                .order_by(MarketBar.ts.desc())
                .limit(1)
            )
            if close is not None:
                marks[symbol] = close
        return marks

    @staticmethod
    def _snapshot_marks(snapshot: MarketSnapshot) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        marks: dict[str, Decimal] = {}
        asks: dict[str, Decimal] = {}
        for symbol, view in snapshot.symbols.items():
            if view.quote is not None and view.quote.bid > 0:
                marks[symbol] = view.quote.mid
                asks[symbol] = view.quote.ask
            elif view.last_bar is not None and view.last_bar.close > 0:
                marks[symbol] = view.last_bar.close
        return marks, asks

    def _set_risk(self, state: RiskState, reason: str) -> None:
        with self._sf.begin() as session:
            now = self._fence(session)
            set_risk_state(session, risk_state=state, reason=reason, actor="system", now=now)

    def _assert_fenced(self) -> None:
        """Prove we still hold the lease before mutating the venue (it has no fence of its own)."""
        with self._sf.begin() as session:
            self._fence(session)

    def _needs_reconcile(self) -> bool:
        """In-session recovery: a stuck risk state or an order in limbo must not wait for the
        end of the day (it would block exits and hold reserved cash)."""
        with self._sf() as session:
            if get_system_state(session).risk_state is RiskState.RECONCILIATION_REQUIRED:
                return True
            stuck = session.scalar(
                select(Order.id)
                .where(
                    Order.account_id == self._account_id,
                    Order.status.in_(_STUCK_STATUSES),
                )
                .limit(1)
            )
            return stuck is not None

    def _current_risk(self) -> RiskState:
        with self._sf() as session:
            return get_system_state(session).risk_state

    # ---- session lifecycle -----------------------------------------------------------------

    def reconcile(self) -> ReconciliationReport:
        return reconcile_account(
            self._sf,
            account_id=self._account_id,
            broker=self._broker,
            clock=self._clock,
            lease=self._lease,
            calendar=self._calendar,
            config=self._config,
        )

    def start_session(self, session_date: date) -> None:
        """Reconcile, then open the session with its start-of-day equity. Idempotent."""
        self.reconcile()
        with self._sf.begin() as session:
            now = self._fence(session)
            try:
                state = self._value(
                    session,
                    marks=self._db_marks(session, now),
                    asks={},
                    now=now,
                    session_date=session_date,
                )
            except MissingMark:
                set_risk_state(
                    session,
                    risk_state=RiskState.UNKNOWN,
                    reason="missing_mark",
                    actor="system",
                    now=now,
                )
                return
            opened = start_session(
                session,
                account_id=self._account_id,
                session_date=session_date,
                start_equity=state.equity,
                now=now,
            )
            if opened:
                record_equity_snapshot(session, state, kind="SESSION_START")
                # The only place approved parameter changes go live: a new session, never a restart.
                if not self._account_name(session).startswith(SCRATCH_ACCOUNT_PREFIXES):
                    apply_approved(session, now=now)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _account_name(self, session: Session) -> str:
        return session.scalar(select(Account.name).where(Account.id == self._account_id)) or ""

    def _ensure_session(self, session_date: date) -> None:
        with self._sf() as session:
            exists = session.scalar(
                select(TradingSession.session_date).where(
                    TradingSession.account_id == self._account_id,
                    TradingSession.session_date == session_date,
                )
            )
        if exists is None:
            self.start_session(session_date)

    def end_session(self, session_date: date) -> None:
        """Expire resting DAY orders, settle up, reconcile, and record what was left open."""
        info = self._calendar.session_on(session_date)
        if info is None:
            return
        self._assert_fenced()
        self._broker.expire_day_orders(session_close=info.close_at)
        self._manager.sync_fills()
        report = self.reconcile()
        with self._sf.begin() as session:
            now = self._fence(session)
            held = {
                symbol: qty
                for symbol, qty in session.execute(
                    select(Instrument.symbol, Position.qty)
                    .join(Position, Position.instrument_id == Instrument.id)
                    .where(Position.account_id == self._account_id, Position.qty > 0)
                )
            }
            valued = True
            try:
                state = self._value(
                    session,
                    marks=self._db_marks(session, now),
                    asks={},
                    now=now,
                    session_date=session_date,
                )
                record_equity_snapshot(session, state, kind="SESSION_END")
            except MissingMark:
                valued = False
            record_audit(
                session,
                actor="system",
                action="session.end",
                target=session_date.isoformat(),
                request_id=None,
                before=None,
                after=None,
                result=report.status,
                now=now,
                details={"unresolved_positions": held, "valued": valued},
            )

    # ---- one minute --------------------------------------------------------------------------

    def run_tick(self, minute: datetime) -> TickResult:
        now = self._clock.now()
        info = self._calendar.session_at(minute - timedelta(seconds=1))
        if info is None:  # not a regular-session minute
            return TickResult(minute, None, 0, 0, 0, self._current_risk())
        session_date = info.session_date
        self._ensure_session(session_date)

        if self.provider is not None:
            with self._sf.begin() as session:
                self._fence(session)
                for ingest in (ingest_bars, ingest_quotes):
                    ingest(
                        session,
                        self.provider,
                        symbols=self._symbols,
                        start=minute - _ONE_MINUTE,
                        end=minute,
                        calendar=self._calendar,
                        clock=self._clock,
                    )
        # Re-read the clock after ingesting: the snapshot only sees rows with ingested_at <= as_of,
        # so an as_of taken before the fetch hides this tick's own fresh bar and quote.
        now = self._clock.now()
        with self._sf.begin() as session:
            self._fence(session)
            snapshot = build_snapshot(
                session,
                symbols=self._symbols,
                as_of=now,
                provider=self._provider_name,
                max_bar_age_seconds=self._config.risk.max_bar_age_seconds,
            )
        quotes = {s: v.quote for s, v in snapshot.symbols.items() if v.quote is not None}
        bars = {s: v.last_bar for s, v in snapshot.symbols.items() if v.last_bar is not None}
        try:
            self._assert_fenced()
            self._broker.process_tick(quotes=quotes, bars=bars)
            fills_applied = self._manager.sync_fills()
            self._manager.cancel_stale_entries(
                ttl=timedelta(seconds=self._config.risk.entry_order_ttl_seconds)
            )
            if self._needs_reconcile():
                self.reconcile()
        except LeaseLost:
            raise
        except TradingError as exc:
            _log.error("tick_sync_failed", error=type(exc).__name__)
            self._set_risk(
                RiskState.RECONCILIATION_REQUIRED, f"tick_sync_failed: {type(exc).__name__}"
            )
            return TickResult(minute, snapshot.id, 0, 0, 0, self._current_risk())

        marks, asks = self._snapshot_marks(snapshot)
        with self._sf.begin() as session:
            now = self._fence(session)
            try:
                state = self._value(
                    session, marks=marks, asks=asks, now=now, session_date=session_date
                )
            except MissingMark:
                set_risk_state(
                    session,
                    risk_state=RiskState.UNKNOWN,
                    reason="missing_mark",
                    actor="system",
                    now=now,
                )
                return TickResult(minute, snapshot.id, fills_applied, 0, 0, RiskState.UNKNOWN)
            snapshot_row_id = record_equity_snapshot(session, state, kind="INTRADAY")
            risk = evaluate_halts(
                session,
                account_id=self._account_id,
                state=state,
                session_date=session_date,
                policy=self._config.risk,
                now=now,
            )

        decisions = submitted = 0
        for request in self._intents.proposals(snapshot=snapshot, portfolio=state, now=now):
            decided, order_id = self._propose(
                request, snapshot, snapshot_row_id, session_date, marks, asks
            )
            decisions += decided
            if order_id is not None:
                self._manager.submit(order_id)
                submitted += 1
        return TickResult(minute, snapshot.id, fills_applied, decisions, submitted, risk)

    def _propose(
        self,
        request: ProposalRequest,
        snapshot: MarketSnapshot,
        portfolio_snapshot_id: UUID,
        session_date: date,
        marks: dict[str, Decimal],
        asks: dict[str, Decimal],
    ) -> tuple[int, UUID | None]:
        instrument = self._instruments.get(request.symbol)
        if instrument is None:
            _log.warning("proposal_for_unknown_symbol", symbol=request.symbol)
            return 0, None
        with self._sf.begin() as session:
            now = self._fence(session)
            already = session.scalar(
                select(RiskDecisionRow.id).where(
                    RiskDecisionRow.account_id == self._account_id,
                    RiskDecisionRow.dedupe_key == request.dedupe_key,
                )
            )
            if already is not None:  # decided before (e.g. before a restart): never re-decide
                return 0, None
            current = self._value_or_fail_closed(
                session, marks=marks, asks=asks, now=now, session_date=session_date
            )
            if current is None:
                return 0, None
            account = build_account_view(
                session,
                account_id=self._account_id,
                portfolio=current,
                session_date=session_date,
                config=self._config,
                ask_by_symbol=asks,
            )
            proposal = ProposedOrder(
                account_id=self._account_id,
                instrument=instrument,
                side=request.side,
                quantity=request.quantity,
                order_type=request.order_type,
                limit_price=request.limit_price,
                time_in_force=TimeInForce.DAY,
                strategy_id=request.strategy_id,
                signal_id=request.signal_id,
                signal_expires_at=request.signal_expires_at,
                dedupe_key=request.dedupe_key,
                strategy_ids=request.strategy_ids,
                attribution=request.attribution,
            )
            recorded = evaluate_and_record(
                session,
                self._engine,
                proposal=proposal,
                account=account,
                market=snapshot,
                portfolio_snapshot_id=portfolio_snapshot_id,
                now=now,
            )
            if not recorded.evaluation.allowed:
                return 1, None
            order_id = self._manager.create_order(
                session,
                proposal=proposal,
                decision=recorded,
                market_snapshot_id=snapshot.id,
                session_date=session_date,
            )
        return 1, order_id
