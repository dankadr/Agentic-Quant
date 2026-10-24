"""End-of-day report: did the data arrive, what did each strategy do, and what did it cost."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.config import AppConfig
from trading.quant.performance import load_report

SCRATCH_PREFIXES = ("replay-", "backtest")


def _account(session: Session) -> tuple[Any, str] | None:
    rows = session.execute(text("SELECT id, name FROM accounts ORDER BY created_at")).all()
    primary = [r for r in rows if not r.name.startswith(SCRATCH_PREFIXES)]
    chosen = (primary or rows)[-1] if (primary or rows) else None
    return (chosen.id, chosen.name) if chosen else None


def daily_report(
    session: Session, day: date, *, config: AppConfig, provider: str
) -> dict[str, Any]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    found = _account(session)
    if found is None:
        return {"date": day.isoformat(), "error": "no account"}
    account_id, account_name = found
    p = {"a": account_id, "s": start, "e": end, "prov": provider}

    ticks = session.execute(
        text(
            "SELECT count(*) FROM equity_snapshots WHERE account_id = :a AND kind = 'INTRADAY'"
            " AND ts >= :s AND ts < :e"
        ),
        p,
    ).scalar_one()
    coverage = {
        u.symbol: {"bars": 0, "expected": ticks, "missing": ticks, "flagged": 0}
        for u in config.universe
    }
    for r in session.execute(
        text(
            "SELECT i.symbol, count(*) AS bars,"
            " count(*) FILTER (WHERE cardinality(b.quality_flags) > 0) AS flagged"
            " FROM market_bars b JOIN instruments i ON i.id = b.instrument_id"
            " WHERE b.provider = :prov AND b.ts >= :s AND b.ts < :e GROUP BY i.symbol"
        ),
        p,
    ):
        if r.symbol in coverage:
            coverage[r.symbol] = {
                "bars": r.bars,
                "expected": ticks,
                "missing": max(ticks - r.bars, 0),
                "flagged": r.flagged,
            }

    edges = {
        r.kind: r.equity
        for r in session.execute(
            text(
                "SELECT DISTINCT ON (kind) kind, equity FROM equity_snapshots WHERE account_id = :a"
                " AND kind IN ('SESSION_START', 'SESSION_END') AND ts >= :s AND ts < :e"
                " ORDER BY kind, ts DESC"
            ),
            p,
        )
    }
    equity_start = edges.get("SESSION_START")
    equity_end = edges.get("SESSION_END")
    if equity_end is None:
        equity_end = session.execute(
            text(
                "SELECT equity FROM equity_snapshots WHERE account_id = :a AND ts >= :s AND ts < :e"
                " ORDER BY ts DESC LIMIT 1"
            ),
            p,
        ).scalar()

    strategies: dict[str, dict[str, Any]] = {}
    for r in session.execute(
        text(
            "SELECT strategy_id, status, count(*) AS n FROM signals"
            " WHERE generated_at >= :s AND generated_at < :e GROUP BY 1, 2"
        ),
        p,
    ):
        strategies.setdefault(r.strategy_id, {"signals": {}, "net_pnl": "0"})["signals"][
            r.status
        ] = r.n
    for r in session.execute(
        text(
            "SELECT a.strategy_id, coalesce(sum(a.pnl), 0) AS pnl FROM strategy_attribution a"
            " JOIN fills f ON f.id = a.fill_id JOIN orders o ON o.id = f.order_id"
            " WHERE o.account_id = :a AND f.ts >= :s AND f.ts < :e GROUP BY 1"
        ),
        p,
    ):
        strategies.setdefault(r.strategy_id, {"signals": {}, "net_pnl": "0"})["net_pnl"] = str(
            r.pnl
        )

    by_status = {
        r.status: r.n
        for r in session.execute(
            text(
                "SELECT status, count(*) AS n FROM orders WHERE account_id = :a"
                " AND created_at >= :s AND created_at < :e GROUP BY 1"
            ),
            p,
        )
    }
    denials = {
        r.code: r.n
        for r in session.execute(
            text(
                "SELECT reasons_json->0->>'code' AS code, count(*) AS n FROM risk_decisions"
                " WHERE NOT allowed AND ts >= :s AND ts < :e GROUP BY 1"
            ),
            p,
        )
    }
    costs = session.execute(
        text(
            "SELECT coalesce(sum(f.fees), 0) AS fees,"
            " coalesce(sum(f.slippage_estimate), 0) AS slip, count(*) AS n"
            " FROM fills f JOIN orders o ON o.id = f.order_id"
            " WHERE o.account_id = :a AND f.ts >= :s AND f.ts < :e"
        ),
        p,
    ).one()

    perf = load_report(session, account_id=account_id, start=start, end=end, provider=provider)
    bench = perf.benchmark
    return {
        "date": day.isoformat(),
        "account": account_name,
        "ticks": ticks,
        "data_coverage": coverage,
        "equity": {
            "start": str(equity_start) if equity_start is not None else None,
            "end": str(equity_end) if equity_end is not None else None,
            "pnl": str(equity_end - equity_start)
            if equity_start is not None and equity_end is not None
            else None,
            "max_drawdown_pct": str(perf.max_drawdown_pct),
        },
        "strategies": strategies,
        "orders": {"total": sum(by_status.values()), "by_status": by_status, "fills": costs.n},
        "risk_denials": denials,
        "costs": {"fees": str(costs.fees), "slippage_estimate": str(costs.slip)},
        "benchmark": {
            "symbol": bench.symbol if bench else "SPY",
            "return_pct": str(bench.return_pct) if bench else None,
        },
    }
