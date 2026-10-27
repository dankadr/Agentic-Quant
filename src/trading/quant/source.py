"""StrategyIntentSource: strategies -> signals -> allocator -> proposals.

Plugs into the tick through the same ``IntentSource`` interface as the scripted source. It only
PROPOSES: every proposal still passes the risk engine and the execution manager. Strategy
parameters are read from the ``strategies`` table, which only changes at session start.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.macro import latest_macro_view
from trading.control.state import RiskState, TradingState, get_system_state
from trading.core.config import AppConfig
from trading.core.hashing import canonical_json
from trading.data.calendar import MarketCalendar
from trading.data.instruments import InstrumentInfo
from trading.data.snapshot import MarketSnapshot
from trading.db.models.research import AllocationDecision, Signal, Strategy
from trading.execution.intents import ProposalRequest
from trading.monitoring.logging import get_logger
from trading.portfolio.valuation import PortfolioState
from trading.quant.allocator import (
    AllocationContext,
    AllocationNote,
    CapitalAllocator,
    SleeveAllocator,
)
from trading.quant.bars import load_session_bars
from trading.quant.flatten import FlattenStrategy
from trading.quant.holdings import (
    load_entered_today,
    load_holdings,
    load_in_flight,
    load_open_buy_by_strategy,
)
from trading.quant.models import SignalProposal, SignalStrategy, StrategyInput
from trading.quant.orb import OrbStrategy
from trading.quant.vwap import VwapStrategy
from trading.risk.service import build_account_view

_log = get_logger(__name__)

StrategyFactory = Callable[..., SignalStrategy]
DEFAULT_FACTORIES: Mapping[str, StrategyFactory] = {
    "orb_v1": OrbStrategy,
    "vwap_v1": VwapStrategy,
    "eod_flatten": FlattenStrategy,
}


def _status(note: AllocationNote | None) -> str:
    if note is None:
        return "IGNORED"
    if note.outcome == "ALLOCATED":
        return "ALLOCATED"
    return "EXPIRED" if note.reason == "EXPIRED" else "IGNORED"


class StrategyIntentSource:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        account_id: Any,
        calendar: MarketCalendar,
        config: AppConfig,
        instruments: Mapping[str, InstrumentInfo],
        provider_name: str,
        factories: Mapping[str, StrategyFactory] = DEFAULT_FACTORIES,
        allocator: CapitalAllocator | None = None,
    ) -> None:
        self._sf = session_factory
        self._account_id = account_id
        self._calendar = calendar
        self._config = config
        self._instruments = instruments
        self._provider = provider_name
        self._factories = factories
        self._allocator = allocator or SleeveAllocator()
        self._symbols = [u.symbol for u in config.universe]

    def _strategies(self, session: Session) -> list[SignalStrategy]:
        rows = session.scalars(
            select(Strategy).where(Strategy.enabled.is_(True)).order_by(Strategy.id)
        )
        out: list[SignalStrategy] = []
        for row in rows:
            cfg = self._config.strategies.get(row.id)
            factory = self._factories.get(row.id)
            if factory is None or cfg is None or cfg.shadow_only or not cfg.enabled:
                continue
            out.append(factory(strategy_id=row.id, version=row.version, params=row.params_json))
        return out

    def proposals(
        self, *, snapshot: MarketSnapshot, portfolio: PortfolioState, now: datetime
    ) -> Sequence[ProposalRequest]:
        info = self._calendar.session_at(now)
        if info is None:
            return ()
        marks: dict[str, Decimal] = {}
        asks: dict[str, Decimal] = {}
        for symbol, view in snapshot.symbols.items():
            if view.quote is not None and view.quote.bid > 0:
                marks[symbol] = view.quote.mid
                asks[symbol] = view.quote.ask
        for position in portfolio.positions:
            marks.setdefault(position.symbol, position.mark)
        with self._sf.begin() as session:
            strategies = self._strategies(session)
            if not strategies:
                return ()
            trading_state = get_system_state(session).trading_state
            data = StrategyInput(
                snapshot=snapshot,
                bars=load_session_bars(
                    session,
                    symbols=self._symbols,
                    session_date=info.session_date,
                    as_of=now,
                    provider=self._provider,
                ),
                holdings=load_holdings(session, self._account_id, now),
                now=now,
                session=info,
                entered_today=load_entered_today(session, self._account_id, info.open_at),
                positions={p.symbol: p.qty for p in portfolio.positions},
                flatten_now=trading_state is TradingState.FLATTENING,
            )
            signals: list[SignalProposal] = []
            for strategy in strategies:
                try:
                    signals.extend(strategy.evaluate(data))
                except Exception:
                    _log.exception("strategy_failed", strategy=strategy.strategy_id)
            if not signals:
                return ()
            exposure = build_account_view(
                session,
                account_id=self._account_id,
                portfolio=portfolio,
                session_date=info.session_date,
                config=self._config,
                ask_by_symbol=asks,
            )
            ctx = AllocationContext(
                equity=portfolio.equity,
                now=now,
                marks=marks,
                holdings=data.holdings,
                held_qty=dict(data.positions),
                open_buy_notional=exposure.open_buy_notional_by_symbol,
                open_sell_qty=exposure.open_sell_qty_by_symbol,
                in_flight=load_in_flight(session, self._account_id),
                open_buy_by_strategy=load_open_buy_by_strategy(session, self._account_id),
                instruments=self._instruments,
                sleeves={n: c.allocation_pct for n, c in self._config.strategies.items()},
                max_symbol_pct=self._config.risk.max_symbol_exposure_pct,
                paper_cfg=self._config.paper_broker,
                entry_scale=self._macro_scale(session, now),
                entries_allowed=(
                    exposure.trading_state is TradingState.PAPER_RUNNING
                    and exposure.risk_state is RiskState.NORMAL
                    and info.close_at - now
                    > timedelta(minutes=self._config.risk.entry_cutoff_minutes_before_close)
                ),
            )
            result = self._allocator.allocate(signals, ctx)
            self._persist(session, snapshot, signals, result.notes, result.targets, now)
        return result.requests

    def _macro_scale(self, session: Session, now: datetime) -> dict[str, Decimal]:
        """Macro risk multiplier for dependent strategies (0 = block new entries)."""
        macro = self._config.agents.macro
        if not macro.enabled:
            return {}
        view = latest_macro_view(session, now)
        if view is not None:
            return dict.fromkeys(macro.dependent_strategies, view.risk_multiplier)
        if macro.required:
            return dict.fromkeys(macro.dependent_strategies, Decimal(0))
        return {}

    def _persist(
        self,
        session: Session,
        snapshot: MarketSnapshot,
        signals: Sequence[SignalProposal],
        notes: Sequence[AllocationNote],
        targets: Mapping[str, Any],
        now: datetime,
    ) -> None:
        by_id = {n.signal_id: n for n in notes}
        for sig in signals:
            instrument = self._instruments.get(sig.symbol)
            if instrument is None:
                continue
            session.execute(
                insert(Signal)
                .values(
                    id=sig.signal_id,
                    strategy_id=sig.strategy_id,
                    strategy_version=sig.strategy_version,
                    instrument_id=instrument.id,
                    generated_at=sig.generated_at,
                    expires_at=sig.expires_at,
                    snapshot_id=snapshot.id,
                    side=sig.side.value,
                    score=None,
                    target_notional=sig.target_notional_usd,
                    stop_price=sig.stop_price,
                    limit_price=sig.limit_price,
                    reason_codes=list(sig.reason_codes),
                    status="PROPOSED",
                )
                .on_conflict_do_nothing()
            )
            session.execute(
                update(Signal)
                .where(Signal.id == sig.signal_id)
                .values(status=_status(by_id.get(sig.signal_id)))
            )
        session.add(
            AllocationDecision(
                ts=now,
                snapshot_id=snapshot.id,
                targets_json=json.loads(canonical_json(dict(targets))),
                rationale_json=json.loads(
                    canonical_json(
                        [
                            {
                                "signal_id": n.signal_id,
                                "strategy_id": n.strategy_id,
                                "symbol": n.symbol,
                                "side": n.side,
                                "outcome": n.outcome,
                                "reason": n.reason,
                                "qty": n.qty,
                            }
                            for n in notes
                        ]
                    )
                ),
            )
        )
        session.flush()
