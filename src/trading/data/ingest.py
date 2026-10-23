"""Idempotent ingestion with quality flags.

Rules: missing bars are never synthesized; suspect data is stored with flags (so research can see
exactly what arrived) but blocking flags keep it out of tradable snapshots; a conflicting
re-delivery never overwrites what is stored, it flags the stored row.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading.core.clock import Clock
from trading.core.time import isoformat_z
from trading.data.calendar import CalendarUnavailable, MarketCalendar, SessionInfo
from trading.data.instruments import InstrumentInfo, load_instruments
from trading.data.models import BAR_INTERVAL, Bar, CorporateActionRecord, QualityFlag, Quote
from trading.data.provider import MarketDataProvider
from trading.db.models.market import CorporateAction, MarketBar, MarketQuote

Flag = QualityFlag


@dataclass
class IngestReport:
    inserted: int = 0
    duplicates: int = 0
    flag_counts: dict[QualityFlag, int] = field(default_factory=dict)
    gaps: list[tuple[str, datetime, datetime]] = field(default_factory=list)
    unknown_symbols: set[str] = field(default_factory=set)

    def count(self, flag: QualityFlag) -> None:
        self.flag_counts[flag] = self.flag_counts.get(flag, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "flag_counts": {f.value: n for f, n in sorted(self.flag_counts.items())},
            "gaps": [[s, isoformat_z(a), isoformat_z(b)] for s, a, b in self.gaps],
            "unknown_symbols": sorted(self.unknown_symbols),
        }


def _session_at(calendar: MarketCalendar, ts: datetime) -> SessionInfo | None:
    try:
        return calendar.session_at(ts)
    except CalendarUnavailable:
        return None  # unknown calendar coverage is treated as outside the session (fail closed)


def _bar_flags(bar: Bar) -> set[QualityFlag]:
    flags: set[QualityFlag] = set()
    prices = (bar.open, bar.high, bar.low, bar.close)
    if any(p <= 0 for p in prices):
        flags.add(Flag.NON_POSITIVE_PRICE)
    elif (
        bar.high < bar.low
        or bar.high < max(bar.open, bar.close)
        or bar.low > min(bar.open, bar.close)
    ):
        flags.add(Flag.OHLC_INCONSISTENT)
    if bar.volume == 0:
        flags.add(Flag.ZERO_VOLUME)
    return flags


def _quote_flags(quote: Quote) -> set[QualityFlag]:
    flags: set[QualityFlag] = set()
    if quote.bid <= 0 or quote.ask <= 0:
        flags.add(Flag.NON_POSITIVE_PRICE)
    elif quote.ask < quote.bid:
        flags.add(Flag.OHLC_INCONSISTENT)  # crossed market
    return flags


def _sorted(flags: set[QualityFlag]) -> list[str]:
    return sorted(f.value for f in flags)


def _with_conflict(current: Sequence[str]) -> list[str]:
    return _sorted({*map(Flag, current), Flag.DUPLICATE_CONFLICT})


def _latest_bar_ts(
    session: Session, instrument_id: UUID, session_date: date, provider: str
) -> datetime | None:
    return session.scalar(
        select(func.max(MarketBar.ts)).where(
            MarketBar.instrument_id == instrument_id,
            MarketBar.session_date == session_date,
            MarketBar.provider == provider,
            MarketBar.interval == "1m",
        )
    )


def ingest_bars(
    session: Session,
    provider: MarketDataProvider,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    calendar: MarketCalendar,
    clock: Clock,
) -> IngestReport:
    instruments = load_instruments(session)
    report = IngestReport()
    now = clock.now()
    previous_yielded: dict[str, datetime] = {}
    last_seen: dict[tuple[str, date], datetime | None] = {}
    for bar in provider.fetch_bars(symbols, start, end):
        instrument = instruments.get(bar.symbol)
        if instrument is None:
            report.unknown_symbols.add(bar.symbol)
            continue
        flags = _bar_flags(bar)
        info = _session_at(calendar, bar.ts)
        session_date = info.session_date if info else None
        if info is None:
            flags.add(Flag.OUTSIDE_SESSION)
        earlier = previous_yielded.get(bar.symbol)
        if earlier is not None and bar.ts < earlier:
            flags.add(Flag.OUT_OF_ORDER)
        previous_yielded[bar.symbol] = bar.ts
        if session_date is not None:
            key = (bar.symbol, session_date)
            if key not in last_seen:
                last_seen[key] = _latest_bar_ts(session, instrument.id, session_date, provider.name)
            seen = last_seen[key]
            if seen is not None and bar.ts - seen > BAR_INTERVAL:
                flags.add(Flag.GAP_BEFORE)
                report.gaps.append((bar.symbol, seen + BAR_INTERVAL, bar.ts))
            last_seen[key] = bar.ts if seen is None else max(seen, bar.ts)
        _store_bar(session, instrument, bar, flags, session_date, now, report)
    return report


def _store_bar(
    session: Session,
    instrument: InstrumentInfo,
    bar: Bar,
    flags: set[QualityFlag],
    session_date: date | None,
    now: datetime,
    report: IngestReport,
) -> None:
    key: dict[str, Any] = {
        "instrument_id": instrument.id,
        "interval": bar.interval,
        "ts": bar.ts,
        "provider": bar.provider,
    }
    inserted = session.execute(
        insert(MarketBar)
        .values(
            **key,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            ingested_at=now,
            session_date=session_date,
            source_seq=bar.source_seq,
            quality_flags=_sorted(flags),
        )
        .on_conflict_do_nothing()
        .returning(MarketBar.ts)
    ).first()
    if inserted is not None:
        report.inserted += 1
        for flag in flags:
            report.count(flag)
        return
    existing = session.execute(
        select(MarketBar).where(*(getattr(MarketBar, k) == v for k, v in key.items()))
    ).scalar_one()
    same = (
        existing.open == bar.open
        and existing.high == bar.high
        and existing.low == bar.low
        and existing.close == bar.close
        and existing.volume == bar.volume
    )
    if same:
        report.duplicates += 1
        return
    report.count(Flag.DUPLICATE_CONFLICT)
    if Flag.DUPLICATE_CONFLICT.value not in existing.quality_flags:
        session.execute(
            update(MarketBar)
            .where(*(getattr(MarketBar, k) == v for k, v in key.items()))
            .values(
                quality_flags=_sorted({*map(Flag, existing.quality_flags), Flag.DUPLICATE_CONFLICT})
            )
        )


def ingest_quotes(
    session: Session,
    provider: MarketDataProvider,
    *,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    calendar: MarketCalendar,
    clock: Clock,
) -> IngestReport:
    instruments = load_instruments(session)
    report = IngestReport()
    now = clock.now()
    previous_yielded: dict[str, datetime] = {}
    for quote in provider.fetch_quotes(symbols, start, end):
        instrument = instruments.get(quote.symbol)
        if instrument is None:
            report.unknown_symbols.add(quote.symbol)
            continue
        flags = _quote_flags(quote)
        if _session_at(calendar, quote.ts) is None:
            flags.add(Flag.OUTSIDE_SESSION)
        earlier = previous_yielded.get(quote.symbol)
        if earlier is not None and quote.ts < earlier:
            flags.add(Flag.OUT_OF_ORDER)
        previous_yielded[quote.symbol] = quote.ts
        key = {"instrument_id": instrument.id, "ts": quote.ts, "provider": quote.provider}
        inserted = session.execute(
            insert(MarketQuote)
            .values(
                **key,
                bid=quote.bid,
                ask=quote.ask,
                bid_size=quote.bid_size,
                ask_size=quote.ask_size,
                ingested_at=now,
                quality_flags=_sorted(flags),
            )
            .on_conflict_do_nothing()
            .returning(MarketQuote.ts)
        ).first()
        if inserted is not None:
            report.inserted += 1
            for flag in flags:
                report.count(flag)
            continue
        existing = session.execute(
            select(MarketQuote).where(*(getattr(MarketQuote, k) == v for k, v in key.items()))
        ).scalar_one()
        if existing.bid == quote.bid and existing.ask == quote.ask:
            report.duplicates += 1
            continue
        report.count(Flag.DUPLICATE_CONFLICT)
        if Flag.DUPLICATE_CONFLICT.value not in existing.quality_flags:
            session.execute(
                update(MarketQuote)
                .where(*(getattr(MarketQuote, k) == v for k, v in key.items()))
                .values(quality_flags=_with_conflict(existing.quality_flags))
            )
    return report


def ingest_corporate_actions(
    session: Session,
    provider: MarketDataProvider,
    *,
    symbols: Sequence[str],
    start: date,
    end: date,
    clock: Clock,
) -> IngestReport:
    instruments = load_instruments(session)
    report = IngestReport()
    now = clock.now()
    action: CorporateActionRecord
    for action in provider.fetch_corporate_actions(symbols, start, end):
        instrument = instruments.get(action.symbol)
        if instrument is None:
            report.unknown_symbols.add(action.symbol)
            continue
        inserted = session.execute(
            insert(CorporateAction)
            .values(
                instrument_id=instrument.id,
                action_type=action.action_type,
                ex_date=action.ex_date,
                ratio_from=action.ratio_from,
                ratio_to=action.ratio_to,
                cash_amount=action.cash_amount,
                provider=action.provider,
                announced_at=action.announced_at,
                ingested_at=now,
            )
            .on_conflict_do_nothing()
            .returning(CorporateAction.id)
        ).first()
        if inserted is None:
            report.duplicates += 1
        else:
            report.inserted += 1
    return report
