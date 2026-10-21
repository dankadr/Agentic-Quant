"""Cash balances and buying-power reservations.

Cash account rules (conservative): debits reduce settled cash immediately; credits (sale
proceeds) count as settled only from their ``settles_on`` date; unsettled funds are never used.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_UP, Decimal
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from trading.core.config import PaperBrokerConfig
from trading.core.enums import Side
from trading.core.errors import TradingError
from trading.core.fees import estimate_fees
from trading.core.money import quantize_cash
from trading.db.models.market import Instrument
from trading.db.models.trading import Order
from trading.execution.state_machine import TERMINAL


class MissingMark(TradingError):
    """A price needed to value a position or reserve cash is unavailable."""


@dataclass(frozen=True)
class CashBalances:
    total: Decimal
    settled: Decimal
    unsettled: Decimal


def cash_balances(session: Session, account_id: UUID, *, as_of_date: date) -> CashBalances:
    row = session.execute(
        text(
            "SELECT coalesce(sum(amount), 0) AS total,"
            " coalesce(sum(amount) FILTER (WHERE amount < 0 OR settles_on <= :d), 0) AS settled"
            " FROM cash_ledger WHERE account_id = :a"
        ),
        {"a": account_id, "d": as_of_date},
    ).one()
    total, settled = quantize_cash(row.total), quantize_cash(row.settled)
    return CashBalances(total=total, settled=settled, unsettled=quantize_cash(total - settled))


def reserved_for_open_buys(
    session: Session,
    account_id: UUID,
    *,
    ask_by_symbol: Mapping[str, Decimal],
    cfg: PaperBrokerConfig,
) -> Decimal:
    """Cash set aside for the unfilled part of every non-terminal BUY order (incl. est. fees)."""
    rows = session.execute(
        select(Order, Instrument.symbol)
        .join(Instrument, Instrument.id == Order.instrument_id)
        .where(
            Order.account_id == account_id,
            Order.side == Side.BUY.value,
            Order.status.not_in([s.value for s in TERMINAL]),
        )
    ).all()
    reserved = Decimal(0)
    for order, symbol in rows:
        price = order.limit_price if order.limit_price is not None else ask_by_symbol.get(symbol)
        if price is None:
            raise MissingMark(f"no reference price to reserve cash for {symbol} order")
        remaining = order.qty - order.filled_qty
        reserved += remaining * price + estimate_fees(Side.BUY, remaining, price, cfg)
    return quantize_cash(reserved, ROUND_UP)
