"""A complete paper-trading environment for execution-manager, reconciliation and replay tests."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.factories import make_risk_decision, make_snapshot
from trading.brokers.base import BrokerOrderRequest, BrokerOrderState
from trading.brokers.paper.broker import PaperBroker
from trading.brokers.paper.faults import FaultPlan
from trading.control.state import (
    RiskState,
    TradingState,
    get_system_state,
    set_risk_state,
    set_trading_state,
)
from trading.core.clock import ManualClock
from trading.core.config import load_config
from trading.core.enums import OrderType, Side, TimeInForce
from trading.core.hashing import canonical_json
from trading.data.calendar import MarketCalendar
from trading.data.instruments import InstrumentInfo, upsert_universe
from trading.data.models import Bar, Quote
from trading.db.models.market import Instrument
from trading.db.models.portfolio import Account
from trading.execution.manager import ExecutionManager
from trading.jobs.lease import EXECUTOR_LEASE, Lease, acquire_lease
from trading.portfolio.ledger import open_paper_account
from trading.risk.halts import start_session
from trading.risk.models import ProposedOrder, RiskEvaluation
from trading.risk.service import RecordedDecision, proposal_terms

CFG = load_config(Path("configs"))
D = Decimal
DAY = date(2026, 10, 19)
NOW = datetime(2026, 10, 19, 15, 0, tzinfo=UTC)
REF = "paper-primary"


class Crash(Exception):
    """Raised by test hooks to simulate a crash between two steps of a workflow."""


class SpyBroker:
    """Wraps a PaperBroker, counting submits and optionally running a hook before each one."""

    def __init__(self, inner: PaperBroker) -> None:
        self.inner = inner
        self.submit_calls = 0
        self.before_submit = None  # type: ignore[var-annotated]
        self.after_submit = None  # type: ignore[var-annotated]

    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrderState:
        self.submit_calls += 1
        if self.before_submit is not None:
            self.before_submit()
        state = self.inner.submit_order(req)
        if self.after_submit is not None:
            self.after_submit()
        return state

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(self.inner, name)


@dataclass
class Env:
    sf: sessionmaker[Session]
    clock: ManualClock
    account_id: UUID
    instruments: dict[str, InstrumentInfo]
    broker: PaperBroker
    spy: SpyBroker
    manager: ExecutionManager
    lease: Lease
    calendar: MarketCalendar
    _n: int = 0

    def place(
        self,
        *,
        symbol: str = "SPY",
        side: Side = Side.BUY,
        qty: str = "0.5",
        limit: str | None = "100.05",
        strategy: str = "orb_v1",
        expires: datetime | None = None,
        allowed: bool = True,
        entry: bool | None = None,
        strategy_ids: tuple[str, ...] = (),
        stored_terms: dict | None = None,  # type: ignore[type-arg]
        decision_age: timedelta = timedelta(0),
    ) -> UUID:
        """Create (not submit) an order backed by a persisted risk decision."""
        self._n += 1
        info = self.instruments[symbol]
        with self.sf.begin() as s:
            account = s.get(Account, self.account_id)
            instrument = s.get(Instrument, info.id)
            assert account is not None
            assert instrument is not None
            snapshot = make_snapshot(s, now=self.clock.now(), data_hash=f"snap-{self._n}")
            terms_proposal = ProposedOrder(
                account_id=self.account_id, instrument=info, side=side, quantity=D(qty),
                order_type=OrderType.LIMIT if limit else OrderType.MARKET,
                limit_price=D(limit) if limit else None, time_in_force=TimeInForce.DAY,
                strategy_id=strategy, strategy_ids=strategy_ids, signal_id=None, signal_expires_at=expires,
                dedupe_key=f"dk-{self._n}",
            )  # fmt: skip
            terms = {
                **__import__("json").loads(canonical_json(proposal_terms(terms_proposal))),
                **(stored_terms or {}),
            }
            row = make_risk_decision(s, account, instrument, snapshot, allowed=allowed,
                                     dedupe_key=f"dk-{self._n}", now=self.clock.now() - decision_age,
                                     proposal_json=terms)  # fmt: skip
            proposal = ProposedOrder(
                account_id=self.account_id, instrument=info, side=side, quantity=D(qty),
                order_type=OrderType.LIMIT if limit else OrderType.MARKET,
                limit_price=D(limit) if limit else None, time_in_force=TimeInForce.DAY,
                strategy_id=strategy, strategy_ids=strategy_ids, signal_id=None, signal_expires_at=expires,
                dedupe_key=f"dk-{self._n}",
            )  # fmt: skip
            decision = RecordedDecision(
                row.id,
                RiskEvaluation(allowed=allowed, reasons=(), limits_version="v1",
                               is_entry=(side is Side.BUY) if entry is None else entry, notional=None),
            )  # fmt: skip
            return self.manager.create_order(s, proposal=proposal, decision=decision,
                                             market_snapshot_id=snapshot.id, session_date=DAY)  # fmt: skip

    def tick(
        self,
        *,
        bid: str = "99.99",
        ask: str = "100.00",
        volume: str = "100000",
        symbol: str = "SPY",
        advance: float = 1.0,
    ):  # type: ignore[no-untyped-def]
        self.clock.advance(timedelta(seconds=advance))
        now = self.clock.now()
        quote = Quote(
            symbol=symbol,
            ts=now,
            bid=D(bid),
            ask=D(ask),
            bid_size=None,
            ask_size=None,
            provider="p",
        )
        bar = Bar(symbol=symbol, interval="1m", ts=now - timedelta(minutes=1), open=D(100), high=D(101),
                  low=D(99), close=D(100), volume=D(volume), provider="p", source_seq=None)  # fmt: skip
        return self.broker.process_tick(quotes={symbol: quote}, bars={symbol: bar})

    def status(self, order_id: UUID) -> str:
        with self.sf() as s:
            return s.execute(
                text("select status from orders where id = :i"), {"i": order_id}
            ).scalar_one()

    def row(self, order_id: UUID, columns: str = "*"):  # type: ignore[no-untyped-def]
        with self.sf() as s:
            return s.execute(
                text(f"select {columns} from orders where id = :i"), {"i": order_id}
            ).one()

    def events(self, order_id: UUID) -> list[tuple[str | None, str]]:
        with self.sf() as s:
            rows = s.execute(
                text(
                    "select from_status, to_status from order_events where order_id = :i order by seq"
                ),
                {"i": order_id},
            ).all()
        return [(r[0], r[1]) for r in rows]

    def scalar(self, sql: str):  # type: ignore[no-untyped-def]
        with self.sf() as s:
            return s.execute(text(sql)).scalar_one()


def build_env(
    db: Session,
    session_factory: sessionmaker[Session],
    clock: ManualClock,
    calendar: MarketCalendar,
    *,
    faults: FaultPlan = FaultPlan(),  # noqa: B008
    running: bool = True,
    risk: RiskState = RiskState.NORMAL,
) -> Env:
    clock.set(NOW)
    instruments = upsert_universe(db, CFG.universe)
    account_id = open_paper_account(db, name=REF, starting_cash=D("1000"), now=NOW)
    start_session(db, account_id=account_id, session_date=DAY, start_equity=D("1000"), now=NOW)
    set_risk_state(db, risk_state=risk, reason="test", actor="t", now=NOW)
    if running:
        set_trading_state(db, target=TradingState.PAPER_RUNNING, expected_version=get_system_state(db).version,
                          actor="t", reason="test", request_id=None, now=NOW)  # fmt: skip
    lease = acquire_lease(db, name=EXECUTOR_LEASE, holder="w1", now=NOW, ttl=timedelta(hours=1))
    assert lease is not None
    db.commit()
    broker = PaperBroker(session_factory, account_ref=REF, config=CFG.paper_broker,
                         instruments=instruments, clock=clock, faults=faults)  # fmt: skip
    broker.open_account(D("1000"))
    spy = SpyBroker(broker)
    manager = ExecutionManager(
        session_factory, spy, clock=clock, lease=lease, calendar=calendar, config=CFG
    )  # type: ignore[arg-type]
    return Env(sf=session_factory, clock=clock, account_id=account_id, instruments=instruments, broker=broker,
               spy=spy, manager=manager, lease=lease, calendar=calendar)  # fmt: skip
