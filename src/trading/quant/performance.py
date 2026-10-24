"""Performance metrics. Pure helpers plus a loader that reads recorded equity, fills and
attribution. All Decimal; undefined ratios are None, never infinity or NaN."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

_ZERO = Decimal(0)
_TRADING_DAYS = Decimal(252)


@dataclass(frozen=True)
class TradeStats:
    trades: int
    wins: int
    win_rate: Decimal | None
    expectancy: Decimal | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    profit_factor: Decimal | None


def max_drawdown(equities: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    """Largest peak-to-trough decline as (absolute, fraction of the peak)."""
    peak: Decimal | None = None
    worst_abs, worst_pct = _ZERO, _ZERO
    for value in equities:
        if peak is None or value > peak:
            peak = value
            continue
        drop = peak - value
        if drop > worst_abs:
            worst_abs = drop
            worst_pct = drop / peak if peak > 0 else _ZERO
    return worst_abs, worst_pct


def trade_stats(pnls: Sequence[Decimal]) -> TradeStats:
    """Per-trade statistics. A trade is a closed round trip (one exit)."""
    n = len(pnls)
    if n == 0:
        return TradeStats(0, 0, None, None, None, None, None)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    gross_win = sum(winners, _ZERO)
    gross_loss = -sum(losers, _ZERO)
    return TradeStats(
        trades=n,
        wins=len(winners),
        win_rate=Decimal(len(winners)) / n,
        expectancy=sum(pnls, _ZERO) / n,
        avg_win=gross_win / len(winners) if winners else None,
        avg_loss=-gross_loss / len(losers) if losers else None,
        profit_factor=gross_win / gross_loss if gross_loss > 0 and winners else None,
    )


def daily_sharpe(returns: Sequence[Decimal]) -> Decimal | None:
    """Annualized mean/std of daily returns (risk-free rate ignored). None if undefined."""
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns, _ZERO) / n
    variance = sum(((r - mean) ** 2 for r in returns), _ZERO) / n
    if variance == 0:
        return None
    return mean / variance.sqrt() * _TRADING_DAYS.sqrt()


@dataclass(frozen=True)
class Costs:
    fees: Decimal
    slippage: Decimal

    @property
    def total(self) -> Decimal:
        return self.fees + self.slippage


@dataclass(frozen=True)
class Benchmark:
    symbol: str
    start_price: Decimal
    end_price: Decimal
    return_pct: Decimal


@dataclass(frozen=True)
class PerformanceReport:
    start: datetime
    end: datetime
    scope: str  # "account" or a strategy id
    curve_kind: str  # "equity" (account) or "cumulative_pnl" (strategy)
    equity_curve: list[tuple[datetime, Decimal]]
    start_equity: Decimal | None
    end_equity: Decimal | None
    net_pnl: Decimal
    return_pct: Decimal | None
    max_drawdown_abs: Decimal
    max_drawdown_pct: Decimal
    trades: TradeStats
    fill_ratio: Decimal | None
    costs: Costs
    benchmark: Benchmark | None = None
    excess_return_pct: Decimal | None = None
    notes: list[str] = field(default_factory=list)


def _benchmark(
    session: Session, symbol: str, start: datetime, end: datetime, provider: str
) -> Benchmark | None:
    rows = session.execute(
        text(
            "SELECT b.open, b.close FROM market_bars b JOIN instruments i ON i.id = b.instrument_id"
            " WHERE i.symbol = :s AND b.provider = :p AND b.ts >= :a AND b.ts < :b"
            " AND NOT (b.quality_flags && ARRAY['NON_POSITIVE_PRICE','OHLC_INCONSISTENT',"
            "'OUTSIDE_SESSION','DUPLICATE_CONFLICT']) ORDER BY b.ts"
        ),
        {"s": symbol, "p": provider, "a": start, "b": end},
    ).all()
    if not rows or rows[0].open <= 0:
        return None
    first, last = rows[0].open, rows[-1].close
    return Benchmark(symbol, first, last, (last - first) / first)


_COSTS_SQL = text(
    "SELECT coalesce(sum(f.fees), 0) AS fees, coalesce(sum(f.slippage_estimate), 0) AS slip"
    " FROM fills f JOIN orders o ON o.id = f.order_id"
    " WHERE o.account_id = :acct AND f.ts >= :a AND f.ts < :b"
    " AND (CAST(:strategy AS text) IS NULL OR EXISTS ("
    "   SELECT 1 FROM strategy_attribution a WHERE a.fill_id = f.id AND a.strategy_id = :strategy))"
)
_FILL_RATIO_SQL = text(
    "SELECT coalesce(sum(o.filled_qty), 0) AS filled, coalesce(sum(o.qty), 0) AS qty"
    " FROM orders o JOIN order_intents n ON n.id = o.intent_id"
    " WHERE o.account_id = :acct AND o.created_at >= :a AND o.created_at < :b"
    " AND o.status NOT IN ('REJECTED', 'CREATED', 'RISK_APPROVED')"
    " AND (CAST(:strategy AS text) IS NULL OR :strategy = ANY (n.strategy_ids))"
)
_EXITS_SQL = text(
    "SELECT a.pnl AS pnl FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
    " JOIN orders o ON o.id = f.order_id"
    " WHERE o.account_id = :acct AND f.ts >= :a AND f.ts < :b AND a.allocated_qty < 0"
    " AND (CAST(:strategy AS text) IS NULL OR a.strategy_id = :strategy)"
    " ORDER BY f.ts, f.execution_id"
)
_EQUITY_SQL = text(
    "SELECT ts, equity, kind FROM equity_snapshots WHERE account_id = :acct"
    " AND ts >= :a AND ts < :b ORDER BY ts, id"
)
_STRATEGY_PNL_SQL = text(
    "SELECT f.ts AS ts, a.pnl AS pnl FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
    " JOIN orders o ON o.id = f.order_id"
    " WHERE o.account_id = :acct AND a.strategy_id = :strategy AND f.ts >= :a AND f.ts < :b"
    " ORDER BY f.ts, f.execution_id"
)


def load_report(
    session: Session,
    *,
    account_id: UUID,
    start: datetime,
    end: datetime,
    strategy_id: str | None = None,
    benchmark_symbol: str = "SPY",
    provider: str = "replay",
) -> PerformanceReport:
    params = {"acct": account_id, "a": start, "b": end, "strategy": strategy_id}
    costs_row = session.execute(_COSTS_SQL, params).one()
    ratio = session.execute(_FILL_RATIO_SQL, params).one()
    exits = [r.pnl for r in session.execute(_EXITS_SQL, params) if r.pnl is not None]
    bench = _benchmark(session, benchmark_symbol, start, end, provider)
    if strategy_id is None:
        snapshots = session.execute(_EQUITY_SQL, params).all()
        curve = [(r.ts, r.equity) for r in snapshots if r.kind in ("INTRADAY", "SESSION_END")]
        start_equity = snapshots[0].equity if snapshots else None
        end_equity = curve[-1][1] if curve else None
        net = _ZERO
        if start_equity is not None and end_equity is not None:
            net = end_equity - start_equity
        ret = net / start_equity if start_equity and start_equity > 0 else None
        kind = "equity"
        values = [v for _, v in curve]
    else:
        running = _ZERO
        curve = []
        for r in session.execute(_STRATEGY_PNL_SQL, params):
            running += r.pnl or _ZERO
            curve.append((r.ts, running))
        start_equity = end_equity = ret = None
        net = running
        kind = "cumulative_pnl"
        values = [_ZERO, *[v for _, v in curve]]
    dd_abs, dd_pct = max_drawdown(values)
    excess = (ret - bench.return_pct) if ret is not None and bench is not None else None
    return PerformanceReport(
        start=start,
        end=end,
        scope=strategy_id or "account",
        curve_kind=kind,
        equity_curve=curve,
        start_equity=start_equity,
        end_equity=end_equity,
        net_pnl=net,
        return_pct=ret,
        max_drawdown_abs=dd_abs,
        max_drawdown_pct=dd_pct,
        trades=trade_stats(exits),
        fill_ratio=ratio.filled / ratio.qty if ratio.qty > 0 else None,
        costs=Costs(costs_row.fees, costs_row.slip),
        benchmark=bench,
        excess_return_pct=excess,
    )
