"""Instrument universe: persisted instruments and quantity increments."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading.core.config import PaperBrokerConfig, UniverseEntry
from trading.core.enums import AssetType
from trading.db.models.market import Instrument

VENUE = "XNYS"


@dataclass(frozen=True)
class InstrumentInfo:
    id: UUID
    symbol: str
    asset_type: AssetType
    tick_size: Decimal
    lot_size: Decimal
    fractional_eligible: bool
    sector: str


def _info(row: Instrument) -> InstrumentInfo:
    return InstrumentInfo(
        id=row.id,
        symbol=row.symbol,
        asset_type=AssetType(row.asset_type),
        tick_size=row.tick_size,
        lot_size=row.lot_size,
        fractional_eligible=row.fractional_eligible,
        sector=row.sector,
    )


def upsert_universe(
    session: Session, universe: Sequence[UniverseEntry]
) -> dict[str, InstrumentInfo]:
    """Insert or update the configured universe; returns it keyed by symbol."""
    for entry in universe:
        values = {
            "symbol": entry.symbol,
            "venue": VENUE,
            "asset_type": entry.asset_type.value,
            "tick_size": entry.tick_size,
            "lot_size": entry.lot_size,
            "fractional_eligible": entry.fractional_eligible,
            "sector": entry.sector,
            "active": True,
        }
        stmt = insert(Instrument).values(**values)
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["symbol", "venue"],
                set_={k: v for k, v in values.items() if k not in ("symbol", "venue")},
            )
        )
    session.flush()
    session.expire_all()
    symbols = [e.symbol for e in universe]
    rows = session.scalars(
        select(Instrument).where(Instrument.symbol.in_(symbols), Instrument.venue == VENUE)
    )
    return {r.symbol: _info(r) for r in rows}


def load_instruments(session: Session) -> dict[str, InstrumentInfo]:
    rows = session.scalars(select(Instrument).where(Instrument.active.is_(True)))
    return {r.symbol: _info(r) for r in rows}


def qty_increment(instrument: InstrumentInfo, cfg: PaperBrokerConfig) -> Decimal:
    """Smallest tradable quantity step: fractional only if both the broker and instrument allow."""
    if cfg.fractional_shares and instrument.fractional_eligible:
        return cfg.fractional_increment
    return instrument.lot_size
