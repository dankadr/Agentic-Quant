"""End-of-day report: did the data arrive, what did each strategy do, and what did it cost."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.core.config import AppConfig
from trading.portfolio.adjustments import dividend_allocations
from trading.quant.performance import capital_flows, load_report


def percentage_display(fraction: Decimal | None) -> str | None:
    return f"{fraction * 100:.4f}%" if fraction is not None else None


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

    snapshots = session.execute(
        text(
            "SELECT ts, kind, equity, realized_pnl, unrealized_pnl FROM equity_snapshots"
            " WHERE account_id=:a AND ts >= :s AND ts < :e ORDER BY ts, id"
        ),
        p,
    ).all()
    starts = [r for r in snapshots if r.kind == "SESSION_START"]
    ends = [r for r in snapshots if r.kind == "SESSION_END"]
    baseline = starts[0] if starts else (snapshots[0] if snapshots else None)
    final = ends[-1] if ends else (snapshots[-1] if snapshots else None)
    equity_start = baseline.equity if baseline else None
    equity_end = final.equity if final else None
    change = final.equity - baseline.equity if baseline and final else None
    flows = (
        capital_flows(session, account_id, start=baseline.ts, end=final.ts)
        if baseline and final
        else None
    )
    pnl = change - flows if change is not None and flows is not None else None
    realized = final.realized_pnl - baseline.realized_pnl if baseline and final else None
    unrealized = final.unrealized_pnl - baseline.unrealized_pnl if baseline and final else None
    ret = pnl / equity_start if pnl is not None and equity_start and equity_start > 0 else None

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
    for at, _, owner, pnl in dividend_allocations(session, account_id, end):
        if at >= start:
            stats = strategies.setdefault(owner, {"signals": {}, "net_pnl": "0"})
            stats["net_pnl"] = str(Decimal(stats["net_pnl"]) + pnl)

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

    diagnostic_reasons: dict[str, dict[str, int]] = {}
    for r in session.execute(
        text(
            "SELECT note->>'strategy_id' AS strategy_id, note->>'reason' AS reason, count(*) AS n"
            " FROM allocation_decisions d CROSS JOIN LATERAL"
            " jsonb_array_elements(CASE WHEN jsonb_typeof(d.rationale_json)='array'"
            " THEN d.rationale_json ELSE '[]'::jsonb END) AS note"
            " WHERE d.ts >= :s AND d.ts < :e AND note->>'outcome'='NO_ENTRY'"
            " GROUP BY 1, 2"
        ),
        p,
    ):
        diagnostic_reasons.setdefault(r.strategy_id, {})[r.reason] = r.n

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
            "pnl": str(pnl) if pnl is not None else None,
            "equity_change": str(change) if change is not None else None,
            "capital_flows": str(flows) if flows is not None else None,
            "realized_pnl_change": str(realized) if realized is not None else None,
            "unrealized_pnl_change": str(unrealized) if unrealized is not None else None,
            "reconciliation_residual": str(pnl - realized - unrealized)
            if pnl is not None and realized is not None and unrealized is not None
            else None,
            "attribution_residual": str(
                realized - sum((Decimal(v["net_pnl"]) for v in strategies.values()), Decimal(0))
            )
            if realized is not None
            else None,
            "return_pct": str(ret) if ret is not None else None,
            "return_pct_display": percentage_display(ret),
            "max_drawdown_pct": str(perf.max_drawdown_pct),
            "max_drawdown_pct_display": percentage_display(perf.max_drawdown_pct),
        },
        "strategies": strategies,
        "orders": {"total": sum(by_status.values()), "by_status": by_status, "fills": costs.n},
        "risk_denials": denials,
        "diagnostic_counts": {
            "sampled_evaluations": sum(sum(v.values()) for v in diagnostic_reasons.values()),
            "by_strategy": diagnostic_reasons,
            "note": "Sampled evaluations, not unique opportunities.",
        },
        "costs": {
            "fees": str(costs.fees),
            "slippage_estimate": str(costs.slip),
            "slippage_deducted_again": False,
            "note": (
                "Fees and fill-price slippage already affect P&L; "
                "slippage is an informational estimate."
            ),
        },
        "benchmark": {
            "symbol": bench.symbol if bench else "SPY",
            "return_pct": str(bench.return_pct) if bench else None,
            "return_pct_display": percentage_display(bench.return_pct if bench else None),
        },
    }
