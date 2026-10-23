"""Portfolio valuation: equity, exposure, P&L and buying power at a point in time."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.config import PaperBrokerConfig
from trading.core.hashing import canonical_json
from trading.core.money import quantize_cash
from trading.db.models.market import Instrument
from trading.db.models.portfolio import EquitySnapshot, Position
from trading.portfolio.balances import MissingMark, cash_balances, reserved_for_open_buys

__all__ = [
    "MissingMark",
    "PortfolioState",
    "PositionView",
    "record_equity_snapshot",
    "value_portfolio",
]


@dataclass(frozen=True)
class PositionView:
    symbol: str
    instrument_id: UUID
    sector: str
    qty: Decimal
    average_cost: Decimal
    mark: Decimal
    market_value: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True)
class PortfolioState:
    account_id: UUID
    as_of: datetime
    cash: Decimal
    settled_cash: Decimal
    reserved_cash: Decimal
    buying_power: Decimal
    equity: Decimal
    gross_exposure: Decimal
    exposure_by_symbol: Mapping[str, Decimal]
    exposure_by_sector: Mapping[str, Decimal]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    positions: tuple[PositionView, ...]


def value_portfolio(
    session: Session,
    account_id: UUID,
    *,
    marks: Mapping[str, Decimal],
    ask_by_symbol: Mapping[str, Decimal],
    as_of: datetime,
    session_date: date,
    cfg: PaperBrokerConfig,
) -> PortfolioState:
    """Value the account. Raises ``MissingMark`` rather than guessing a price."""
    rows = session.execute(
        select(Position, Instrument)
        .join(Instrument, Instrument.id == Position.instrument_id)
        .where(Position.account_id == account_id)
        .order_by(Instrument.symbol)
    ).all()
    views: list[PositionView] = []
    realized = Decimal(0)
    for position, instrument in rows:
        realized += position.realized_pnl
        if position.qty <= 0:
            continue
        mark = marks.get(instrument.symbol)
        if mark is None:
            raise MissingMark(f"no mark for held position {instrument.symbol}")
        market_value = quantize_cash(position.qty * mark)
        views.append(
            PositionView(
                symbol=instrument.symbol,
                instrument_id=instrument.id,
                sector=instrument.sector,
                qty=position.qty,
                average_cost=position.average_cost,
                mark=mark,
                market_value=market_value,
                unrealized_pnl=quantize_cash((mark - position.average_cost) * position.qty),
            )
        )
    balances = cash_balances(session, account_id, as_of_date=session_date)
    reserved = reserved_for_open_buys(session, account_id, ask_by_symbol=ask_by_symbol, cfg=cfg)
    gross = sum((v.market_value for v in views), Decimal(0))
    by_sector: dict[str, Decimal] = {}
    for v in views:
        by_sector[v.sector] = by_sector.get(v.sector, Decimal(0)) + v.market_value
    return PortfolioState(
        account_id=account_id,
        as_of=as_of,
        cash=balances.total,
        settled_cash=balances.settled,
        reserved_cash=reserved,
        buying_power=balances.settled - reserved,  # unsettled funds are never used
        equity=balances.total + gross,
        gross_exposure=quantize_cash(gross),
        exposure_by_symbol={v.symbol: v.market_value for v in views},
        exposure_by_sector={k: quantize_cash(v) for k, v in by_sector.items()},
        realized_pnl=quantize_cash(realized),
        unrealized_pnl=quantize_cash(sum((v.unrealized_pnl for v in views), Decimal(0))),
        positions=tuple(views),
    )


def record_equity_snapshot(
    session: Session,
    state: PortfolioState,
    *,
    kind: Literal["INTRADAY", "SESSION_START", "SESSION_END"],
) -> UUID:
    positions = {
        v.symbol: {
            "qty": v.qty,
            "average_cost": v.average_cost,
            "mark": v.mark,
            "market_value": v.market_value,
            "unrealized_pnl": v.unrealized_pnl,
        }
        for v in state.positions
    }
    row = EquitySnapshot(
        account_id=state.account_id,
        ts=state.as_of,
        kind=kind,
        cash=state.cash,
        settled_cash=state.settled_cash,
        equity=state.equity,
        exposure=state.gross_exposure,
        realized_pnl=state.realized_pnl,
        unrealized_pnl=state.unrealized_pnl,
        positions_json=json.loads(canonical_json(positions)),
    )
    session.add(row)
    session.flush()
    return row.id
