"""Point-in-time market snapshots.

A snapshot contains only what a decision-maker could have known at ``as_of``: bars that had
closed AND had been ingested by then, quotes that existed AND had been ingested by then. This is
the look-ahead guard for both replay and live paper trading. The persisted ``data_hash`` covers
the data only (never database ids), so identical inputs hash identically across fresh databases.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.core.hashing import canonical_json, content_hash
from trading.core.time import isoformat_z
from trading.data.instruments import load_instruments
from trading.data.models import BAR_INTERVAL, BLOCKING_FLAGS, Bar, QualityFlag, Quote
from trading.db.models.market import MarketBar, MarketQuote, MarketSnapshotRow

QualityStatus = Literal["OK", "DEGRADED", "STALE"]


@dataclass(frozen=True)
class SymbolView:
    symbol: str
    instrument_id: UUID
    last_bar: Bar | None
    quote: Quote | None
    bar_age_seconds: int | None
    stale: bool
    tradable: bool
    bar_flags: tuple[str, ...] = ()
    quote_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketSnapshot:
    id: UUID
    as_of: datetime
    provider: str
    symbols: Mapping[str, SymbolView]
    quality_status: QualityStatus
    data_hash: str


def _blocking(flags: Sequence[str]) -> bool:
    return any(QualityFlag(f) in BLOCKING_FLAGS for f in flags)


def _to_bar(row: MarketBar | None, symbol: str, provider: str) -> Bar | None:
    if row is None:
        return None
    return Bar(
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


def _to_quote(row: MarketQuote | None, symbol: str, provider: str) -> Quote | None:
    if row is None:
        return None
    return Quote(
        symbol=symbol,
        ts=row.ts,
        bid=row.bid,
        ask=row.ask,
        bid_size=row.bid_size,
        ask_size=row.ask_size,
        provider=provider,
    )


def _view_json(view: SymbolView) -> dict[str, Any]:
    return {
        "last_bar": view.last_bar.model_dump(mode="json") if view.last_bar else None,
        "quote": view.quote.model_dump(mode="json") if view.quote else None,
        "bar_age_seconds": view.bar_age_seconds,
        "stale": view.stale,
        "tradable": view.tradable,
        "bar_flags": list(view.bar_flags),
        "quote_flags": list(view.quote_flags),
    }


def build_snapshot(
    session: Session,
    *,
    symbols: Sequence[str],
    as_of: datetime,
    provider: str,
    max_bar_age_seconds: int,
) -> MarketSnapshot:
    """Build and persist a snapshot of the latest visible bar and quote per symbol."""
    instruments = load_instruments(session)
    views: dict[str, SymbolView] = {}
    for symbol in symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            raise ValueError(f"unknown instrument {symbol!r}")
        bar_row = session.scalars(
            select(MarketBar)
            .where(
                MarketBar.instrument_id == instrument.id,
                MarketBar.interval == "1m",
                MarketBar.provider == provider,
                MarketBar.ts <= as_of - BAR_INTERVAL,
                MarketBar.ingested_at <= as_of,
            )
            .order_by(MarketBar.ts.desc())
            .limit(1)
        ).first()
        quote_row = session.scalars(
            select(MarketQuote)
            .where(
                MarketQuote.instrument_id == instrument.id,
                MarketQuote.provider == provider,
                MarketQuote.ts <= as_of,
                MarketQuote.ingested_at <= as_of,
            )
            .order_by(MarketQuote.ts.desc())
            .limit(1)
        ).first()
        bar = _to_bar(bar_row, symbol, provider)
        quote = _to_quote(quote_row, symbol, provider)
        age = None if bar is None else as_of - bar.available_at
        stale = bar is None or age is None or age > timedelta(seconds=max_bar_age_seconds)
        bar_flags = tuple(bar_row.quality_flags) if bar_row else ()
        views[symbol] = SymbolView(
            symbol=symbol,
            instrument_id=instrument.id,
            last_bar=bar,
            quote=quote,
            bar_age_seconds=None if age is None else int(age.total_seconds()),
            stale=stale,
            tradable=not stale and not _blocking(bar_flags),
            bar_flags=bar_flags,
            quote_flags=tuple(quote_row.quality_flags) if quote_row else (),
        )
    tradable = [v.tradable for v in views.values()]
    status: QualityStatus = (
        "OK" if tradable and all(tradable) else "STALE" if not any(tradable) else "DEGRADED"
    )
    hashed = {
        "as_of": isoformat_z(as_of),
        "provider": provider,
        "quality_status": status,
        "symbols": {s: _view_json(v) for s, v in views.items()},
    }
    stored = {**hashed, "instrument_ids": {s: str(v.instrument_id) for s, v in views.items()}}
    row = MarketSnapshotRow(
        as_of=as_of,
        provider=provider,
        data_hash=content_hash(hashed),
        quality_status=status,
        content_json=json.loads(canonical_json(stored)),
    )
    session.add(row)
    session.flush()
    return MarketSnapshot(
        id=row.id,
        as_of=as_of,
        provider=provider,
        symbols=views,
        quality_status=status,
        data_hash=row.data_hash,
    )


def load_snapshot(session: Session, snapshot_id: UUID) -> MarketSnapshot:
    row = session.get(MarketSnapshotRow, snapshot_id)
    if row is None:
        raise LookupError(f"snapshot {snapshot_id} not found")
    content = row.content_json
    views = {}
    for symbol, data in content["symbols"].items():
        views[symbol] = SymbolView(
            symbol=symbol,
            instrument_id=UUID(content["instrument_ids"][symbol]),
            last_bar=Bar.model_validate(data["last_bar"]) if data["last_bar"] else None,
            quote=Quote.model_validate(data["quote"]) if data["quote"] else None,
            bar_age_seconds=data["bar_age_seconds"],
            stale=data["stale"],
            tradable=data["tradable"],
            bar_flags=tuple(data["bar_flags"]),
            quote_flags=tuple(data["quote_flags"]),
        )
    return MarketSnapshot(
        id=row.id,
        as_of=row.as_of,
        provider=row.provider,
        symbols=views,
        quality_status=row.quality_status,  # type: ignore[arg-type]
        data_hash=row.data_hash,
    )
