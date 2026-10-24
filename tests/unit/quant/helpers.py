from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from trading.data.calendar import MarketCalendar
from trading.data.models import Bar, Quote
from trading.data.snapshot import MarketSnapshot, SymbolView
from trading.quant.models import Holding, StrategyInput

D = Decimal
OPEN = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)


def bars_from(
    rows: Sequence[tuple[str, str, str, str, str]], symbol: str = "SPY", start: datetime = OPEN
) -> tuple[Bar, ...]:
    """rows: (open, high, low, close, volume), one per consecutive minute from ``start``."""
    return tuple(
        Bar(symbol=symbol, interval="1m", ts=start + timedelta(minutes=i), open=D(o), high=D(h), low=D(lo), close=D(c),
            volume=D(v), provider="p", source_seq=None)
        for i, (o, h, lo, c, v) in enumerate(rows)
    )  # fmt: skip


def view(symbol: str, now: datetime, *, bid: str, ask: str, tradable: bool = True) -> SymbolView:
    quote = Quote(
        symbol=symbol, ts=now, bid=D(bid), ask=D(ask), bid_size=None, ask_size=None, provider="p"
    )
    return SymbolView(symbol=symbol, instrument_id=uuid4(), last_bar=None, quote=quote, bar_age_seconds=0,
                      stale=not tradable, tradable=tradable)  # fmt: skip


def make_input(
    calendar: MarketCalendar,
    bars: dict[str, tuple[Bar, ...]],
    now: datetime,
    *,
    quotes: dict[str, tuple[str, str]] | None = None,
    holdings: tuple[Holding, ...] = (),
    entered: frozenset[tuple[str, str]] = frozenset(),
    untradable: frozenset[str] = frozenset(),
    positions: dict[str, Decimal] | None = None,
) -> StrategyInput:
    quotes = quotes if quotes is not None else {s: ("100.29", "100.31") for s in bars}
    views = {
        s: view(s, now, bid=quotes[s][0], ask=quotes[s][1], tradable=s not in untradable)
        for s in bars
        if s in quotes
    }
    snapshot = MarketSnapshot(
        id=uuid4(), as_of=now, provider="p", symbols=views, quality_status="OK", data_hash="h"
    )
    session = calendar.session_at(now)
    assert session is not None
    return StrategyInput(
        snapshot=snapshot,
        bars=bars,
        holdings=holdings,
        now=now,
        session=session,
        entered_today=entered,
        positions=positions or {},
    )


def orb_rows(
    *, breakout_close: str = "100.30", breakout_volume: str = "1500", or_minutes: int = 15
):  # type: ignore[no-untyped-def]
    """15 opening-range bars (high 100.10, low 99.90, volume 1000) then one post-range bar."""
    rows = [("100.00", "100.10", "99.90", "100.00", "1000")] * or_minutes
    rows.append(("100.05", "100.35", "100.05", breakout_close, breakout_volume))
    return rows
