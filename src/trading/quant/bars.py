"""Point-in-time session bars for strategies. Same visibility rule as market snapshots:
a bar is visible only once it has closed AND had been ingested by ``as_of``."""

from collections.abc import Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.data.models import BAR_INTERVAL, BLOCKING_FLAGS, Bar, QualityFlag
from trading.db.models.market import Instrument, MarketBar


def load_session_bars(
    session: Session,
    *,
    symbols: Sequence[str],
    session_date: date,
    as_of: datetime,
    provider: str,
) -> dict[str, tuple[Bar, ...]]:
    """Today's usable 1-minute bars per symbol, oldest first. Missing minutes stay missing."""
    result: dict[str, tuple[Bar, ...]] = {s: () for s in symbols}
    rows = session.execute(
        select(MarketBar, Instrument.symbol)
        .join(Instrument, Instrument.id == MarketBar.instrument_id)
        .where(
            Instrument.symbol.in_(list(symbols)),
            MarketBar.session_date == session_date,
            MarketBar.provider == provider,
            MarketBar.interval == "1m",
            MarketBar.ts <= as_of - BAR_INTERVAL,
            MarketBar.ingested_at <= as_of,
        )
        .order_by(Instrument.symbol, MarketBar.ts)
    ).all()
    collected: dict[str, list[Bar]] = {s: [] for s in symbols}
    for row, symbol in rows:
        if any(QualityFlag(f) in BLOCKING_FLAGS for f in row.quality_flags):
            continue  # corrupt data never feeds an indicator
        collected[symbol].append(
            Bar(
                symbol=symbol,
                interval="1m",
                ts=row.ts,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                provider=provider,
                source_seq=row.source_seq,
            )
        )
    for symbol, bars in collected.items():
        result[symbol] = tuple(bars)
    return result
