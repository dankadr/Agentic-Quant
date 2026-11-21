"""PaperBroker: a simulated external venue with its own state in the ``paper_venue`` schema.

It uses its own transactions only and derives cash and positions from its own fills, never from
the application ledger, so reconciliation compares two independent records.
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
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
from trading.core.enums import OrderType, PositionEffect, Side, effective_effect
from trading.core.money import is_multiple_of, quantize_cash, quantize_price, trade_cash_amount
from trading.data.calendar import NEW_YORK, MarketCalendar, XnysCalendar, settlement_date
from trading.data.instruments import InstrumentInfo, qty_increment
from trading.data.models import Bar, Quote
from trading.db.models.market import CorporateAction
from trading.db.models.venue import VenueAccount, VenueAction, VenueFill, VenueOrder

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
        allow_short: bool = False,
        calendar: MarketCalendar | None = None,
        faults: FaultPlan = FaultPlan(),  # noqa: B008  (frozen, immutable default)
    ) -> None:
        self._sf = session_factory
        self._ref = account_ref
        self._cfg = config
        self._instruments = instruments
        self._clock = clock
        self._calendar = calendar
        self._settlement_cutoff: tuple[date, date] | None = None
        self._allow_short = allow_short
        self._faults = faults
        self._quotes: dict[str, Quote] = {}

    # ---- helpers -------------------------------------------------------------------------

    def _guard(self) -> None:
        if self._faults.unavailable:
            raise BrokerUnavailable("paper broker unavailable (injected)")

    def _account(self, session: Session, *, lock: bool = False) -> VenueAccount:
        account = session.get(VenueAccount, self._ref, with_for_update=lock)
        if account is None:
            raise BrokerRejected("UNKNOWN_ACCOUNT")
        return account

    def _settled_through(self, today: date) -> date:
        """Latest sale date whose proceeds have settled by this exchange-local day."""
        if self._settlement_cutoff is not None and self._settlement_cutoff[0] == today:
            return self._settlement_cutoff[1]
        days = self._cfg.settlement_days
        span = timedelta(days=30 + 3 * days)
        calendar = self._calendar or XnysCalendar(today - span, today + span)
        trade_day = today
        while settlement_date(calendar, trade_day, days) > today:
            trade_day -= timedelta(days=1)
        self._settlement_cutoff = (today, trade_day)
        return trade_day

    def _ledger(
        self, session: Session, *, settled_only: bool = False
    ) -> tuple[Decimal, dict[str, Decimal], dict[str, Decimal]]:
        cash = self._account(session).starting_cash
        actions = session.scalars(
            select(VenueAction).where(VenueAction.account_ref == self._ref)
        ).all()
        cash += sum((action.cash_delta for action in actions), Decimal(0))
        positions: dict[str, Decimal] = {}
        costs: dict[str, Decimal] = {}
        fills = session.execute(
            select(VenueFill, VenueOrder.position_effect)
            .join(VenueOrder, VenueOrder.client_order_id == VenueFill.client_order_id)
            .where(VenueFill.account_ref == self._ref)
            .order_by(VenueFill.seq)
        )
        for fill, raw_effect in fills:
            side = Side(fill.side)
            effect = effective_effect(side, PositionEffect(raw_effect) if raw_effect else None)
            cash -= fill.fees
            if (
                not settled_only
                or effect is not PositionEffect.CLOSE_LONG
                or fill.ts.astimezone(NEW_YORK).date()
                <= self._settled_through(self._clock.now().astimezone(NEW_YORK).date())
            ):
                cash += trade_cash_amount(side, fill.qty, fill.price)
            adjusted_qty, adjusted_price = fill.qty, fill.price
            for action in actions:
                if fill.symbol == action.symbol and fill.seq <= action.last_fill_seq:
                    adjusted_qty = adjusted_qty * action.ratio_to / action.ratio_from
                    adjusted_price = adjusted_price * action.ratio_from / action.ratio_to
            held = positions.get(fill.symbol, Decimal(0))
            if side is Side.SELL and held <= 0:
                costs[fill.symbol] = quantize_price(
                    (abs(held) * costs.get(fill.symbol, Decimal(0)) + adjusted_qty * adjusted_price)
                    / (abs(held) + adjusted_qty)
                )
            positions[fill.symbol] = held + (adjusted_qty if side is Side.BUY else -adjusted_qty)
        return cash, {s: q for s, q in positions.items() if q != 0}, costs

    def apply_action(
        self, session: Session, *, action: CorporateAction, symbol: str, expected_qty: Decimal
    ) -> None:
        self._account(session, lock=True)
        if session.get(VenueAction, (self._ref, action.id)):
            return
        open_order = session.scalar(
            select(VenueOrder.client_order_id)
            .where(
                VenueOrder.account_ref == self._ref,
                VenueOrder.symbol == symbol,
                VenueOrder.status.in_(_OPEN),
            )
            .limit(1)
        )
        if open_order:
            raise BrokerRejected("CORPORATE_ACTION_OPEN_ORDER")
        _, positions, _ = self._ledger(session)
        if positions.get(symbol, Decimal(0)) != expected_qty:
            raise BrokerRejected("CORPORATE_ACTION_POSITION_MISMATCH")
        seq = session.scalar(
            select(func.max(VenueFill.seq)).where(VenueFill.account_ref == self._ref)
        )
        session.add(
            VenueAction(
                account_ref=self._ref,
                action_id=action.id,
                symbol=symbol,
                last_fill_seq=seq or 0,
                ratio_from=action.ratio_from or Decimal(1),
                ratio_to=action.ratio_to or Decimal(1),
                cash_delta=quantize_cash(
                    positions.get(symbol, Decimal(0)) * (action.cash_amount or Decimal(0))
                ),
            )
        )
        session.flush()

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

    def _fresh_ask(self, symbol: str) -> Decimal | None:
        quote = self._quotes.get(symbol)
        if quote is None or quote.bid <= 0 or quote.ask < quote.bid:
            return None
        age = (self._clock.now() - quote.ts).total_seconds()
        return quote.ask if 0 <= age <= self._cfg.quote_max_age_seconds else None

    def _reservation_price(self, row: VenueOrder) -> Decimal | None:
        if row.limit_price is not None:
            return row.limit_price
        return self._fresh_ask(row.symbol)

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
            cash, positions, _ = self._ledger(session)
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
            try:
                effect = effective_effect(req.side, req.position_effect)
            except ValueError as exc:
                raise BrokerRejected("INVALID_POSITION_EFFECT") from exc
            self._check_capacity(session, req.symbol, effect, req.qty, req.limit_price)
            row = VenueOrder(
                client_order_id=coid,
                broker_order_id=f"B-{coid}",
                account_ref=self._ref,
                symbol=req.symbol,
                side=req.side.value,
                position_effect=req.position_effect,
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

    def _check_capacity(
        self,
        session: Session,
        symbol: str,
        effect: PositionEffect,
        qty: Decimal,
        limit: Decimal | None,
        *,
        filling: VenueOrder | None = None,
        fee: Decimal | None = None,
    ) -> None:
        cash, positions, costs = self._ledger(session, settled_only=effect.is_entry)
        held = positions.get(symbol, Decimal(0))
        directed = -held if effect.is_short else held
        if effect is PositionEffect.OPEN_SHORT and not self._allow_short:
            raise BrokerRejected("SHORT_DISABLED")
        if directed < 0:
            raise BrokerRejected("INSUFFICIENT_POSITION")
        rows = session.scalars(
            select(VenueOrder).where(
                VenueOrder.account_ref == self._ref, VenueOrder.status.in_(_OPEN)
            )
        )
        reserved = Decimal(0)
        pending_exit = Decimal(0)
        for row in rows:
            pending = effective_effect(
                Side(row.side), PositionEffect(row.position_effect) if row.position_effect else None
            )
            remaining = row.qty - row.filled_qty
            if row is filling:
                remaining -= qty
            if remaining <= 0:
                continue
            if row.symbol == symbol:
                if pending.is_short != effect.is_short:
                    raise BrokerRejected("INSUFFICIENT_POSITION")
                if pending == effect and not effect.is_entry:
                    pending_exit += remaining
            if effect.is_entry and pending.is_entry:
                price = self._entry_price(row.symbol, pending, row.limit_price)
                reserved += remaining * price + estimate_fees(
                    pending.side, remaining, price, self._cfg
                )
            elif effect.is_entry and pending is PositionEffect.CLOSE_SHORT:
                cover_price = self._reservation_price(row)
                if cover_price is None:
                    raise BrokerRejected("NO_REFERENCE_PRICE")
                reserved += estimate_fees(pending.side, remaining, cover_price, self._cfg)
        if not effect.is_entry:
            if qty + pending_exit > directed:
                raise BrokerRejected("INSUFFICIENT_POSITION")
            return
        for held_symbol, shares in positions.items():
            if shares < 0:
                ask = self._fresh_ask(held_symbol)
                if ask is None:
                    raise BrokerRejected("NO_REFERENCE_PRICE")
                basis = costs[held_symbol]
                reserved += -shares * (basis + max(basis, ask))
        price = self._entry_price(symbol, effect, limit)
        required_fee = fee if fee is not None else estimate_fees(effect.side, qty, price, self._cfg)
        if cash - reserved < qty * price + required_fee:
            raise BrokerRejected("INSUFFICIENT_FUNDS")

    def _entry_price(self, symbol: str, effect: PositionEffect, limit: Decimal | None) -> Decimal:
        ask = self._fresh_ask(symbol)
        if effect.is_short and ask is None:
            raise BrokerRejected("NO_REFERENCE_PRICE")
        price = limit if limit is not None else ask
        if price is None:
            raise BrokerRejected("NO_REFERENCE_PRICE")
        return max(price, ask or price) if effect.is_short else price

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
            .order_by(VenueFill.seq)
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
        self._quotes.update(quotes)
        now = self._clock.now()
        produced: list[BrokerFill] = []
        with self._sf.begin() as session:
            self._account(session, lock=True)
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
                effect = effective_effect(
                    side, PositionEffect(row.position_effect) if row.position_effect else None
                )
                try:
                    self._check_capacity(
                        session,
                        row.symbol,
                        effect,
                        decision.qty,
                        decision.price,
                        filling=row,
                        fee=decision.fees,
                    )
                except BrokerRejected:
                    continue
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
                session.flush()
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
