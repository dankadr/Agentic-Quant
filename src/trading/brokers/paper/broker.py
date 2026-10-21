"""PaperBroker: a simulated external venue with its own state in the ``paper_venue`` schema.

It uses its own transactions only and derives cash and positions from its own fills, never from
the application ledger, so reconciliation compares two independent records.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from trading.brokers.base import (
    BrokerAccountState,
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerRejected,
    BrokerTimeout,
    BrokerUnavailable,
)
from trading.brokers.paper.faults import FaultPlan
from trading.brokers.paper.fill_model import VenueOrderView, estimate_fees, evaluate_fill
from trading.core.clock import Clock
from trading.core.config import PaperBrokerConfig
from trading.core.enums import OrderType, Side
from trading.core.money import is_multiple_of, quantize_price, trade_cash_amount
from trading.data.instruments import InstrumentInfo, qty_increment
from trading.data.models import Bar, Quote
from trading.db.models.venue import VenueAccount, VenueFill, VenueOrder

_OPEN = (BrokerOrderStatus.ACCEPTED.value, BrokerOrderStatus.PARTIALLY_FILLED.value)


class PaperBroker:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        account_ref: str,
        config: PaperBrokerConfig,
        instruments: Mapping[str, InstrumentInfo],
        clock: Clock,
        faults: FaultPlan = FaultPlan(),  # noqa: B008  (frozen, immutable default)
    ) -> None:
        self._sf = session_factory
        self._ref = account_ref
        self._cfg = config
        self._instruments = instruments
        self._clock = clock
        self._faults = faults
        self._last_ask: dict[str, Decimal] = {}

    # ---- helpers -------------------------------------------------------------------------

    def _guard(self) -> None:
        if self._faults.unavailable:
            raise BrokerUnavailable("paper broker unavailable (injected)")

    def _account(self, session: Session, *, lock: bool = False) -> VenueAccount:
        account = session.get(VenueAccount, self._ref, with_for_update=lock)
        if account is None:
            raise BrokerRejected("UNKNOWN_ACCOUNT")
        return account

    def _ledger(self, session: Session) -> tuple[Decimal, dict[str, Decimal]]:
        """Cash and positions derived from this venue's own fills."""
        cash = self._account(session).starting_cash
        positions: dict[str, Decimal] = {}
        fills = session.scalars(
            select(VenueFill)
            .where(VenueFill.account_ref == self._ref)
            .order_by(VenueFill.ts, VenueFill.execution_id)
        )
        for f in fills:
            side = Side(f.side)
            cash += trade_cash_amount(side, f.qty, f.price) - f.fees
            delta = f.qty if side is Side.BUY else -f.qty
            positions[f.symbol] = positions.get(f.symbol, Decimal(0)) + delta
        return cash, {s: q for s, q in positions.items() if q != 0}

    def _state(self, session: Session, row: VenueOrder) -> BrokerOrderState:
        fills = session.scalars(
            select(VenueFill).where(VenueFill.client_order_id == row.client_order_id)
        ).all()
        total = sum((f.qty for f in fills), Decimal(0))
        avg = (
            quantize_price(sum((f.qty * f.price for f in fills), Decimal(0)) / total)
            if total
            else None
        )
        return BrokerOrderState(
            client_order_id=row.client_order_id,
            broker_order_id=row.broker_order_id,
            status=BrokerOrderStatus(row.status),
            qty=row.qty,
            filled_qty=row.filled_qty,
            avg_fill_price=avg,
            updated_at=row.updated_at,
        )

    def _reservation_price(self, row: VenueOrder) -> Decimal | None:
        if row.limit_price is not None:
            return row.limit_price
        return self._last_ask.get(row.symbol)

    # ---- account -------------------------------------------------------------------------

    def open_account(self, starting_cash: Decimal) -> None:
        """Create the venue account once; later calls never change existing starting cash."""
        with self._sf.begin() as session:
            session.execute(
                insert(VenueAccount)
                .values(account_ref=self._ref, starting_cash=starting_cash)
                .on_conflict_do_nothing()
            )

    def get_account(self) -> BrokerAccountState:
        self._guard()
        with self._sf() as session:
            cash, positions = self._ledger(session)
        return BrokerAccountState(cash=cash, positions=positions, as_of=self._clock.now())

    # ---- orders --------------------------------------------------------------------------

    def submit_order(self, req: BrokerOrderRequest) -> BrokerOrderState:
        self._guard()
        coid = req.client_order_id
        if coid in self._faults.timeout_before_accept:
            raise BrokerTimeout(f"injected timeout before accepting {coid}")
        now = self._clock.now()
        with self._sf.begin() as session:
            self._account(session, lock=True)
            if session.get(VenueOrder, coid) is not None:
                raise BrokerRejected("DUPLICATE_CLIENT_ORDER_ID")
            instrument = self._instruments.get(req.symbol)
            if instrument is None:
                raise BrokerRejected("UNKNOWN_SYMBOL")
            if req.qty <= 0 or not is_multiple_of(req.qty, qty_increment(instrument, self._cfg)):
                raise BrokerRejected("INVALID_QUANTITY")
            if req.order_type is OrderType.LIMIT:
                price = req.limit_price
                if price is None or price <= 0 or not is_multiple_of(price, instrument.tick_size):
                    raise BrokerRejected("INVALID_TICK_SIZE")
            self._check_capacity(session, req)
            row = VenueOrder(
                client_order_id=coid,
                broker_order_id=f"B-{coid}",
                account_ref=self._ref,
                symbol=req.symbol,
                side=req.side.value,
                type=req.order_type.value,
                tif=req.time_in_force.value,
                limit_price=req.limit_price,
                qty=req.qty,
                filled_qty=Decimal(0),
                status=BrokerOrderStatus.ACCEPTED.value,
                submitted_at=now,
                eligible_at=now + timedelta(milliseconds=self._cfg.latency_ms),
                fill_seq=0,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            state = self._state(session, row)
        if coid in self._faults.timeout_after_accept:
            raise BrokerTimeout(f"injected timeout after accepting {coid}")
        return state

    def _check_capacity(self, session: Session, req: BrokerOrderRequest) -> None:
        cash, positions = self._ledger(session)
        open_rows = session.scalars(
            select(VenueOrder).where(
                VenueOrder.account_ref == self._ref, VenueOrder.status.in_(_OPEN)
            )
        ).all()
        if req.side is Side.BUY:
            ref_price = (
                req.limit_price if req.limit_price is not None else self._last_ask.get(req.symbol)
            )
            if ref_price is None:
                raise BrokerRejected("NO_REFERENCE_PRICE")
            required = req.qty * ref_price + estimate_fees(Side.BUY, req.qty, ref_price, self._cfg)
            reserved = Decimal(0)
            for row in open_rows:
                if row.side != Side.BUY.value:
                    continue
                price = self._reservation_price(row)
                if price is None:
                    raise BrokerRejected("NO_REFERENCE_PRICE")
                remaining = row.qty - row.filled_qty
                reserved += remaining * price + estimate_fees(Side.BUY, remaining, price, self._cfg)
            if cash - reserved < required:
                raise BrokerRejected("INSUFFICIENT_FUNDS")
        else:
            open_sells = sum(
                (
                    r.qty - r.filled_qty
                    for r in open_rows
                    if r.side == Side.SELL.value and r.symbol == req.symbol
                ),
                Decimal(0),
            )
            if req.qty > positions.get(req.symbol, Decimal(0)) - open_sells:
                raise BrokerRejected("INSUFFICIENT_POSITION")

    def cancel_order(self, client_order_id: str) -> BrokerOrderState:
        self._guard()
        with self._sf.begin() as session:
            row = session.get(VenueOrder, client_order_id, with_for_update=True)
            if row is None:
                raise BrokerRejected("UNKNOWN_ORDER")
            if row.status in _OPEN:
                row.status = BrokerOrderStatus.CANCELED.value
                row.updated_at = self._clock.now()
                session.flush()
            return self._state(session, row)

    def get_order(self, client_order_id: str) -> BrokerOrderState | None:
        self._guard()
        with self._sf() as session:
            row = session.get(VenueOrder, client_order_id)
            return None if row is None else self._state(session, row)

    def list_open_orders(self) -> Sequence[BrokerOrderState]:
        self._guard()
        with self._sf() as session:
            rows = session.scalars(
                select(VenueOrder)
                .where(VenueOrder.account_ref == self._ref, VenueOrder.status.in_(_OPEN))
                .order_by(VenueOrder.client_order_id)
            ).all()
            return [self._state(session, r) for r in rows]

    def list_fills(self, *, since: datetime | None = None) -> Sequence[BrokerFill]:
        self._guard()
        stmt = (
            select(VenueFill, VenueOrder.broker_order_id)
            .join(VenueOrder, VenueOrder.client_order_id == VenueFill.client_order_id)
            .where(VenueFill.account_ref == self._ref)
            .order_by(VenueFill.ts, VenueFill.execution_id)
        )
        if since is not None:
            stmt = stmt.where(VenueFill.ts >= since)
        with self._sf() as session:
            return [
                BrokerFill(
                    execution_id=f.execution_id,
                    client_order_id=f.client_order_id,
                    broker_order_id=broker_order_id,
                    symbol=f.symbol,
                    side=Side(f.side),
                    qty=f.qty,
                    price=f.price,
                    fees=f.fees,
                    slippage_estimate=f.slippage_estimate,
                    ts=f.ts,
                )
                for f, broker_order_id in session.execute(stmt).all()
            ]

    # ---- simulation ----------------------------------------------------------------------

    def process_tick(
        self, *, quotes: Mapping[str, Quote], bars: Mapping[str, Bar]
    ) -> list[BrokerFill]:
        """Advance the simulation one tick: evaluate every open order against current data."""
        self._guard()
        for symbol, quote in quotes.items():
            if quote.ask > 0:
                self._last_ask[symbol] = quote.ask
        now = self._clock.now()
        produced: list[BrokerFill] = []
        with self._sf.begin() as session:
            self._account(session, lock=True)
            cash, positions = self._ledger(session)
            rows = session.scalars(
                select(VenueOrder)
                .where(VenueOrder.account_ref == self._ref, VenueOrder.status.in_(_OPEN))
                .order_by(VenueOrder.client_order_id)
                .with_for_update()
            ).all()
            for row in rows:
                instrument = self._instruments[row.symbol]
                view = VenueOrderView(
                    client_order_id=row.client_order_id,
                    symbol=row.symbol,
                    side=Side(row.side),
                    order_type=OrderType(row.type),
                    limit_price=row.limit_price,
                    qty=row.qty,
                    filled_qty=row.filled_qty,
                    eligible_at=row.eligible_at,
                    fill_seq=row.fill_seq,
                )
                decision = evaluate_fill(
                    view,
                    quote=quotes.get(row.symbol),
                    bar=bars.get(row.symbol),
                    now=now,
                    instrument=instrument,
                    cfg=self._cfg,
                )
                if decision is None:
                    continue
                side = Side(row.side)
                cash_delta = trade_cash_amount(side, decision.qty, decision.price) - decision.fees
                held = positions.get(row.symbol, Decimal(0))
                if (side is Side.BUY and cash + cash_delta < 0) or (
                    side is Side.SELL and decision.qty > held
                ):
                    continue  # the venue never fills beyond cash or position
                session.add(
                    VenueFill(
                        execution_id=decision.execution_id,
                        client_order_id=row.client_order_id,
                        account_ref=self._ref,
                        symbol=row.symbol,
                        side=row.side,
                        ts=now,
                        qty=decision.qty,
                        price=decision.price,
                        fees=decision.fees,
                        slippage_estimate=decision.slippage_estimate,
                    )
                )
                row.filled_qty += decision.qty
                row.fill_seq += 1
                row.updated_at = now
                row.status = (
                    BrokerOrderStatus.FILLED.value
                    if row.filled_qty >= row.qty
                    else BrokerOrderStatus.PARTIALLY_FILLED.value
                )
                cash += cash_delta
                positions[row.symbol] = held + (decision.qty if side is Side.BUY else -decision.qty)
                produced.append(
                    BrokerFill(
                        execution_id=decision.execution_id,
                        client_order_id=row.client_order_id,
                        broker_order_id=row.broker_order_id,
                        symbol=row.symbol,
                        side=side,
                        qty=decision.qty,
                        price=decision.price,
                        fees=decision.fees,
                        slippage_estimate=decision.slippage_estimate,
                        ts=now,
                    )
                )
        return produced

    def expire_day_orders(self, *, session_close: datetime) -> int:
        """Expire every still-open DAY order submitted at or before ``session_close``."""
        self._guard()
        now = self._clock.now()
        with self._sf.begin() as session:
            result = session.execute(
                update(VenueOrder)
                .where(
                    VenueOrder.account_ref == self._ref,
                    VenueOrder.status.in_(_OPEN),
                    VenueOrder.submitted_at <= session_close,
                )
                .values(status=BrokerOrderStatus.EXPIRED.value, updated_at=now)
                .returning(VenueOrder.client_order_id)
            )
            return len(result.all())
