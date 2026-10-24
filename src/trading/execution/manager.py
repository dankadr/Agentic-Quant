"""The execution manager: the ONLY writer of order intents and orders.

Submission is two-phase so a crash can never produce a duplicate order:
  A. (txn) verify fence + risk decision + system state, CREATED -> RISK_APPROVED -> SUBMITTING;
  broker call outside any transaction;
  B. (txn) record the outcome. A timeout/failure becomes UNKNOWN and is resolved by
     reconciliation against the broker, never by resubmitting.
Every write transaction first asserts the executor lease fence.
"""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.base import (
    BrokerAdapter,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.control.state import RiskState, SystemState, TradingState, get_system_state
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.core.enums import OrderType, Side, TimeInForce
from trading.core.errors import TradingError
from trading.core.hashing import canonical_json
from trading.core.ids import make_client_order_id
from trading.data.calendar import NEW_YORK, MarketCalendar
from trading.db.models.market import Instrument
from trading.db.models.portfolio import TradingSession
from trading.db.models.trading import FillRow, Order, OrderEvent, OrderIntent, RiskDecisionRow
from trading.execution.state_machine import OPEN_AT_BROKER, OrderStatus
from trading.execution.transitions import transition_order
from trading.jobs.lease import Lease, assert_fence
from trading.jobs.outbox import emit_event
from trading.monitoring.logging import get_logger
from trading.monitoring.metrics import ORDERS_BY_STATUS
from trading.portfolio.ledger import apply_fill
from trading.risk.models import ProposedOrder
from trading.risk.service import RecordedDecision, proposal_terms

_log = get_logger(__name__)
_STRATEGY_ID = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_FILL_OVERLAP = timedelta(minutes=1)
_MAX_DECISION_AGE = timedelta(minutes=5)


class RiskNotApproved(TradingError):
    """No valid, matching, allowed persisted risk decision backs this order."""


class DecisionAlreadyUsed(TradingError):
    """A risk decision authorises exactly one order intent."""


class InvalidStrategyId(TradingError):
    """Strategy ids are embedded in client order ids, so they use a strict charset (no '-')."""


class SessionNotStarted(TradingError):
    """No trading session row exists for the session date."""


@dataclass(frozen=True)
class _Outcome:
    status: OrderStatus
    reason: str | None
    broker_order_id: str | None = None


def _gate_reason(state: SystemState, is_entry: bool) -> str | None:
    """Why the CURRENT system state forbids submitting now (re-checked at submit time)."""
    if state.trading_state is not TradingState.PAPER_RUNNING:
        return "TRADING_PAUSED"
    if state.risk_state is RiskState.UNKNOWN:
        return "RISK_STATE_UNKNOWN"
    if state.risk_state is RiskState.RECONCILIATION_REQUIRED:
        return "RECONCILIATION_REQUIRED"
    if is_entry and state.risk_state is RiskState.HALTED_DAILY_LOSS:
        return "DAILY_LOSS_LIMIT"
    if is_entry and state.risk_state is RiskState.HALTED_DRAWDOWN:
        return "DRAWDOWN_LIMIT"
    return None


class ExecutionManager:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        broker: BrokerAdapter,
        *,
        clock: Clock,
        lease: Lease,
        calendar: MarketCalendar,
        config: AppConfig,
    ) -> None:
        self._sf = session_factory
        self._broker = broker
        self._clock = clock
        self._lease = lease
        self._calendar = calendar
        self._config = config

    def _fence(self, session: Session) -> datetime:
        now = self._clock.now()
        assert_fence(session, self._lease, now=now)
        return now

    # ---- creation ------------------------------------------------------------------------

    def create_order(
        self,
        session: Session,
        *,
        proposal: ProposedOrder,
        decision: RecordedDecision,
        market_snapshot_id: UUID,
        session_date: date,
    ) -> UUID:
        """Persist the immutable intent and a CREATED order, in the caller's transaction."""
        now = self._fence(session)
        for strategy in proposal.strategies:
            if not _STRATEGY_ID.fullmatch(strategy):
                raise InvalidStrategyId(f"invalid strategy id {strategy!r}")
        row = session.get(RiskDecisionRow, decision.decision_id)
        if row is None or not row.allowed or not decision.evaluation.allowed:
            raise RiskNotApproved("risk decision is not an allowed, persisted decision")
        if row.account_id != proposal.account_id:
            raise RiskNotApproved("risk decision account mismatch")
        if row.instrument_id != proposal.instrument.id:
            raise RiskNotApproved("risk decision instrument mismatch")
        if row.market_snapshot_id != market_snapshot_id:
            raise RiskNotApproved("risk decision market snapshot mismatch")
        if row.dedupe_key != proposal.dedupe_key:
            raise RiskNotApproved("risk decision dedupe key mismatch")
        if now - row.ts > _MAX_DECISION_AGE:
            raise RiskNotApproved("risk decision too old to act on")
        expected = json.loads(canonical_json(proposal_terms(proposal)))
        if {key: row.proposal_json.get(key) for key in expected} != expected:
            raise RiskNotApproved("order terms differ from the terms the risk engine evaluated")
        if (
            session.scalar(select(OrderIntent.id).where(OrderIntent.risk_decision_id == row.id))
            is not None
        ):
            raise DecisionAlreadyUsed(f"risk decision {row.id} already has an order intent")

        trading_session = session.scalars(
            select(TradingSession)
            .where(
                TradingSession.account_id == proposal.account_id,
                TradingSession.session_date == session_date,
            )
            .with_for_update()
        ).one_or_none()
        if trading_session is None:
            raise SessionNotStarted(f"no trading session for {session_date}")

        prefix = f"paper-{proposal.strategy_id}-{session_date:%Y%m%d}-"
        existing = session.scalar(
            select(func.count())
            .select_from(OrderIntent)
            .where(OrderIntent.client_order_id.startswith(prefix, autoescape=True))
        )
        client_order_id = make_client_order_id(
            proposal.strategy_id, session_date, (existing or 0) + 1
        )
        is_entry = proposal.side is Side.BUY
        intent = OrderIntent(
            account_id=proposal.account_id,
            instrument_id=proposal.instrument.id,
            side=proposal.side.value,
            quantity=proposal.quantity,
            order_type=proposal.order_type.value,
            limit_price=proposal.limit_price,
            time_in_force=proposal.time_in_force.value,
            strategy_ids=list(proposal.strategies),
            market_snapshot_id=market_snapshot_id,
            risk_decision_id=row.id,
            client_order_id=client_order_id,
            expires_at=proposal.signal_expires_at,
            is_entry=is_entry,
            created_at=now,
        )
        try:
            with (
                session.begin_nested()
            ):  # SAVEPOINT: a failed insert must not poison the caller's txn
                session.add(intent)
                session.flush()
        except IntegrityError as exc:
            reused = session.scalar(
                select(OrderIntent.id).where(OrderIntent.risk_decision_id == row.id)
            )
            if reused is not None:
                raise DecisionAlreadyUsed(
                    f"risk decision {row.id} already has an order intent"
                ) from exc
            raise  # any other constraint failure (e.g. an id collision) must surface as itself
        order = Order(
            account_id=proposal.account_id,
            intent_id=intent.id,
            instrument_id=proposal.instrument.id,
            client_order_id=client_order_id,
            broker_order_id=None,
            side=proposal.side.value,
            type=proposal.order_type.value,
            time_in_force=proposal.time_in_force.value,
            limit_price=proposal.limit_price,
            qty=proposal.quantity,
            filled_qty=0,
            avg_fill_price=None,
            status=OrderStatus.CREATED.value,
            status_reason=None,
            version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        session.add(
            OrderEvent(
                order_id=order.id, ts=now, from_status=None, to_status="CREATED", reason="created"
            )
        )
        if is_entry:
            trading_session.new_entries += 1
        emit_event(
            session,
            event_type="order_created",
            payload={"client_order_id": client_order_id, "side": proposal.side.value},
            now=now,
        )
        ORDERS_BY_STATUS.labels(OrderStatus.CREATED.value).inc()
        return order.id

    # ---- submission ----------------------------------------------------------------------

    def submit(self, order_id: UUID) -> OrderStatus:
        """Submit a CREATED order exactly once. Any other status returns without a broker call."""
        request, early = self._prepare_submit(order_id)
        if request is None:
            return early if early is not None else OrderStatus.UNKNOWN
        with self._sf.begin() as session:
            self._fence(session)  # last check before leaving the transaction boundary
        outcome = self._call_broker(request)
        return self._record_outcome(order_id, outcome)

    def _prepare_submit(
        self, order_id: UUID
    ) -> tuple[BrokerOrderRequest | None, OrderStatus | None]:
        with self._sf.begin() as session:
            now = self._fence(session)
            order = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).one_or_none()
            if order is None:
                raise LookupError(f"order {order_id} not found")
            if order.status != OrderStatus.CREATED.value:
                return None, OrderStatus(order.status)
            intent = session.get(OrderIntent, order.intent_id)
            decision = session.get(RiskDecisionRow, intent.risk_decision_id) if intent else None
            instrument = session.get(Instrument, order.instrument_id)
            if intent is None or decision is None or instrument is None:
                raise RiskNotApproved("order has no intent/decision")
            reject: str | None = None
            if not decision.allowed:
                reject = "RISK_NOT_APPROVED"
            elif intent.expires_at is not None and now >= intent.expires_at:
                reject = "SIGNAL_EXPIRED"
            else:
                reject = _gate_reason(get_system_state(session), intent.is_entry)
            if reject is not None:
                transition_order(session, order, OrderStatus.REJECTED, reason=reject, now=now)
                return None, OrderStatus.REJECTED
            transition_order(session, order, OrderStatus.RISK_APPROVED, reason=None, now=now)
            transition_order(session, order, OrderStatus.SUBMITTING, reason=None, now=now)
            return (
                BrokerOrderRequest(
                    client_order_id=order.client_order_id,
                    symbol=instrument.symbol,
                    side=Side(order.side),
                    order_type=OrderType(order.type),
                    qty=order.qty,
                    limit_price=order.limit_price,
                    time_in_force=TimeInForce(order.time_in_force),
                ),
                None,
            )

    def _call_broker(self, request: BrokerOrderRequest) -> _Outcome:
        try:
            state: BrokerOrderState = self._broker.submit_order(request)
        except BrokerRejected as exc:
            return _Outcome(OrderStatus.REJECTED, exc.reason)
        except BrokerTimeout:
            return _Outcome(OrderStatus.UNKNOWN, "BROKER_TIMEOUT")
        except BrokerUnavailable:
            return _Outcome(OrderStatus.UNKNOWN, "BROKER_UNAVAILABLE")
        if state.status is BrokerOrderStatus.REJECTED:
            return _Outcome(OrderStatus.REJECTED, "BROKER_REJECTED")
        # Fills (even immediate ones) are applied by sync_fills, the single fill path.
        return _Outcome(OrderStatus.ACKNOWLEDGED, None, state.broker_order_id)

    def _record_outcome(self, order_id: UUID, outcome: _Outcome) -> OrderStatus:
        with self._sf.begin() as session:
            now = self._fence(session)  # LeaseLost here leaves SUBMITTING for the new leader
            order = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).one()
            if order.status != OrderStatus.SUBMITTING.value:
                return OrderStatus(order.status)
            transition_order(
                session,
                order,
                outcome.status,
                reason=outcome.reason,
                now=now,
                broker_order_id=outcome.broker_order_id,
            )
            return outcome.status

    # ---- fills / cancels -----------------------------------------------------------------

    def _settles_on(self, ts: datetime) -> date:
        info = self._calendar.session_at(ts)
        day = info.session_date if info is not None else ts.astimezone(NEW_YORK).date()
        for _ in range(self._config.paper_broker.settlement_days):
            day = self._calendar.next_session_after(day).session_date
        return day

    def sync_fills(self) -> int:
        """Import broker fills (idempotent) and pick up broker-side cancels/expiries."""
        with self._sf() as session:
            latest = session.scalar(select(func.max(FillRow.ts)))
        since = None if latest is None else latest - _FILL_OVERLAP
        applied = 0
        for fill in self._broker.list_fills(since=since):
            with self._sf.begin() as session:
                now = self._fence(session)
                order = session.scalars(
                    select(Order).where(Order.client_order_id == fill.client_order_id)
                ).one_or_none()
                if order is None:
                    _log.warning("fill_for_unknown_order", execution_id=fill.execution_id)
                    continue
                result = apply_fill(
                    session,
                    account_id=order.account_id,
                    fill=fill,
                    settles_on=self._settles_on(fill.ts),
                    now=now,
                )
                applied += result is not None
        self._sync_terminal_states()
        return applied

    def _sync_terminal_states(self) -> None:
        with self._sf() as session:
            open_orders = session.execute(
                select(Order.id, Order.client_order_id).where(
                    Order.status.in_([s.value for s in OPEN_AT_BROKER])
                )
            ).all()
        for order_id, client_order_id in open_orders:
            state = self._broker.get_order(client_order_id)
            if state is None or state.status not in (
                BrokerOrderStatus.CANCELED,
                BrokerOrderStatus.EXPIRED,
            ):
                continue
            target = (
                OrderStatus.CANCELED
                if state.status is BrokerOrderStatus.CANCELED
                else OrderStatus.EXPIRED
            )
            with self._sf.begin() as session:
                now = self._fence(session)
                order = session.scalars(
                    select(Order).where(Order.id == order_id).with_for_update()
                ).one()
                if order.status in {s.value for s in OPEN_AT_BROKER}:
                    transition_order(
                        session, order, target, reason="BROKER_" + target.value, now=now
                    )

    def cancel(self, order_id: UUID, *, reason: str) -> OrderStatus:
        """Cancel an order where that is safe; orders in limbo wait for reconciliation."""
        with self._sf.begin() as session:
            now = self._fence(session)
            order = session.scalars(
                select(Order).where(Order.id == order_id).with_for_update()
            ).one_or_none()
            if order is None:
                raise LookupError(f"order {order_id} not found")
            status = OrderStatus(order.status)
            if status is OrderStatus.CREATED:
                transition_order(session, order, OrderStatus.REJECTED, reason=reason, now=now)
                return OrderStatus.REJECTED
            if status is OrderStatus.RISK_APPROVED:
                transition_order(session, order, OrderStatus.CANCELED, reason=reason, now=now)
                return OrderStatus.CANCELED
            if status not in OPEN_AT_BROKER:
                return status  # terminal, or SUBMITTING/UNKNOWN which reconciliation owns
            try:
                broker_state = self._broker.cancel_order(order.client_order_id)
            except (BrokerRejected, BrokerTimeout, BrokerUnavailable) as exc:
                _log.warning("cancel_failed", order_id=str(order_id), error=type(exc).__name__)
                return status
            if broker_state.status is BrokerOrderStatus.CANCELED:
                transition_order(session, order, OrderStatus.CANCELED, reason=reason, now=now)
                return OrderStatus.CANCELED
            return status
