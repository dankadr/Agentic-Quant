"""Assembles the paper-trading runtime. Only the worker (and offline replay) may call this:
it is the one place a broker adapter is constructed."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.paper.broker import PaperBroker
from trading.brokers.paper.faults import FaultPlan
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.data.calendar import MarketCalendar
from trading.data.instruments import upsert_universe
from trading.data.provider import MarketDataProvider
from trading.db.models.portfolio import Account
from trading.execution.intents import IntentSource
from trading.execution.manager import ExecutionManager
from trading.execution.tick import PaperTickProcessor
from trading.jobs.lease import Lease
from trading.portfolio.ledger import open_paper_account
from trading.quant.registry import seed_strategies
from trading.quant.source import StrategyIntentSource
from trading.risk.engine import DeterministicRiskEngine


@dataclass(frozen=True)
class TradingRuntime:
    account_id: UUID
    broker: PaperBroker
    manager: ExecutionManager
    processor: PaperTickProcessor
    calendar: MarketCalendar


def build_runtime(
    *,
    session_factory: sessionmaker[Session],
    config: AppConfig,
    clock: Clock,
    lease: Lease,
    calendar: MarketCalendar,
    provider: MarketDataProvider | None,
    provider_name: str = "replay",
    account_name: str = "paper-primary",
    intents: IntentSource | None = None,
    faults: FaultPlan | None = None,
) -> TradingRuntime:
    """Get-or-create the paper account and venue, and wire the processor around them."""
    with session_factory.begin() as session:
        instruments = upsert_universe(session, config.universe)
        seed_strategies(session, config, now=clock.now())
        account_id = session.scalar(select(Account.id).where(Account.name == account_name))
        if account_id is None:
            account_id = open_paper_account(
                session,
                name=account_name,
                starting_cash=config.system.starting_cash_usd,
                now=clock.now(),
            )
    broker = PaperBroker(
        session_factory,
        account_ref=account_name,
        config=config.paper_broker,
        instruments=instruments,
        clock=clock,
        faults=faults or FaultPlan(),
    )
    broker.open_account(config.system.starting_cash_usd)
    intent_source = intents or StrategyIntentSource(
        session_factory,
        account_id=account_id,
        calendar=calendar,
        config=config,
        instruments=instruments,
        provider_name=provider_name,
    )
    manager = ExecutionManager(
        session_factory, broker, clock=clock, lease=lease, calendar=calendar, config=config
    )
    engine = DeterministicRiskEngine(
        config.risk,
        calendar=calendar,
        paper_cfg=config.paper_broker,
        limits_version=config.limits_version,
    )
    processor = PaperTickProcessor(
        session_factory,
        account_id=account_id,
        broker=broker,
        manager=manager,
        engine=engine,
        provider=provider,
        provider_name=provider_name,
        intents=intent_source,
        instruments=instruments,
        calendar=calendar,
        config=config,
        clock=clock,
        lease=lease,
    )
    return TradingRuntime(
        account_id=account_id,
        broker=broker,
        manager=manager,
        processor=processor,
        calendar=calendar,
    )
