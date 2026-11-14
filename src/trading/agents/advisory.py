"""Deterministic research advisers. Their only writes are auditable agent-run results.

Activity ranks are descriptive, not expected returns or recommendations to trade. Every query
is bounded by the observation time and ingestion time; no later bars or quotes are available.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from trading.core.config import AppConfig
from trading.core.hashing import canonical_json, content_hash
from trading.core.time import isoformat_z
from trading.data.calendar import MarketCalendar
from trading.db.models.market import Instrument, MarketBar, MarketQuote
from trading.db.models.research import AgentRun
from trading.monitoring.report import _account, daily_report

PROMPT_VERSION = "advisory-v1"
_ZERO = Decimal(0)
_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class ActivityCandidate:
    symbol: str
    session_return: Decimal
    relative_volume: Decimal
    spread_bps: Decimal
    data_at: datetime


def quality_findings(
    coverage: Mapping[str, Mapping[str, Any]], *, quote_max_age_seconds: int
) -> list[str]:
    findings: list[str] = []
    for symbol, item in sorted(coverage.items()):
        expected = item["expected"]
        if expected and Decimal(item["bars"]) / expected < Decimal("0.95"):
            findings.append(f"{symbol}: coverage below 95%")
        if item["flagged"]:
            findings.append(f"{symbol}: flagged bars present")
        age = item["quote_age_seconds"]
        if age is None or age > quote_max_age_seconds:
            findings.append(f"{symbol}: missing or stale quote")
    return findings


def rank_activity(candidates: Sequence[ActivityCandidate]) -> list[dict[str, Any]]:
    def percentile(value: Decimal, values: Sequence[Decimal]) -> Decimal:
        if len(values) <= 1:
            return _ZERO
        return Decimal(sum(v <= value for v in values) - 1) / (len(values) - 1)

    changes = [abs(c.session_return) for c in candidates]
    volumes = [c.relative_volume for c in candidates]
    ranked = [
        (
            c,
            (percentile(abs(c.session_return), changes) + percentile(c.relative_volume, volumes))
            / 2,
        )
        for c in candidates
    ]
    ranked.sort(key=lambda pair: (-pair[1], pair[0].symbol))
    return [
        {
            "symbol": c.symbol,
            "rank": rank,
            "score": str(score),
            "session_return": str(c.session_return),
            "session_return_percent": str(c.session_return * 100),
            "relative_volume": str(c.relative_volume),
            "spread_bps": str(c.spread_bps),
            "direction": "UP"
            if c.session_return > 0
            else "DOWN"
            if c.session_return < 0
            else "FLAT",
            "data_at": isoformat_z(c.data_at),
        }
        for rank, (c, score) in enumerate(ranked[:5], start=1)
    ]


def _store(
    sf: sessionmaker[Session],
    name: str,
    now: datetime,
    result: dict[str, Any],
    *,
    reason: str | None = None,
) -> str:
    status = "SKIPPED" if reason else "COMPLETED"
    result = {
        "agent_name": name,
        "created_at": isoformat_z(now),
        "prompt_version": PROMPT_VERSION,
        "authority": "RESEARCH_ONLY",
        **result,
    }
    if reason:
        result.update(skip_reason=reason, conclusion=f"Skipped: {reason}")
    result["input_hash"] = content_hash(result)
    with sf.begin() as session:
        session.add(
            AgentRun(
                agent_name=name,
                model="deterministic",
                prompt_version=PROMPT_VERSION,
                started_at=now,
                completed_at=now,
                status=status,
                cost_usd=_ZERO,
                result_json=json.loads(canonical_json(result)),
            )
        )
    return status


def _market(
    session: Session,
    config: AppConfig,
    provider: str,
    start: datetime,
    end: datetime,
    now: datetime,
) -> tuple[dict[str, list[MarketBar]], dict[str, MarketQuote]]:
    bars: dict[str, list[MarketBar]] = {u.symbol: [] for u in config.universe}
    for symbol, bar in session.execute(
        select(Instrument.symbol, MarketBar)
        .join(Instrument)
        .where(
            MarketBar.provider == provider,
            MarketBar.interval == "1m",
            MarketBar.ts >= start,
            MarketBar.ts < end,
            MarketBar.ingested_at <= now,
        )
        .order_by(MarketBar.ts)
    ):
        if symbol in bars:
            bars[symbol].append(bar)
    quotes: dict[str, MarketQuote] = {}
    for symbol, quote in session.execute(
        select(Instrument.symbol, MarketQuote)
        .join(Instrument)
        .where(
            MarketQuote.provider == provider,
            MarketQuote.ts <= now,
            MarketQuote.ts >= start - timedelta(minutes=5),
            MarketQuote.ingested_at <= now,
        )
        .order_by(MarketQuote.ts)
    ):
        if symbol in bars:
            quotes[symbol] = quote
    return bars, quotes


def _quote_valid(quote: MarketQuote | None, now: datetime, max_age: int) -> bool:
    return bool(
        quote is not None
        and quote.bid > 0
        and quote.ask >= quote.bid
        and not quote.quality_flags
        and (now - quote.ts).total_seconds() <= max_age
    )


def run_data_quality_agent(
    session_factory: sessionmaker[Session],
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    now: datetime,
) -> str:
    name = "data_quality"
    info = calendar.session_at(now)
    if info is None:
        return _store(session_factory, name, now, {}, reason="outside_session")
    end = now.replace(second=0, microsecond=0)
    start = max(info.open_at, end - timedelta(minutes=30))
    expected = int((end - start).total_seconds() // 60)
    if not expected:
        return _store(session_factory, name, now, {}, reason="no_closed_bars")
    with session_factory() as session:
        bars, quotes = _market(session, config, provider_name, start, end, now)
        coverage = {
            symbol: {
                "bars": len(rows),
                "expected": expected,
                "flagged": sum(bool(b.quality_flags) for b in rows),
                "quote_age_seconds": (now - quotes[symbol].ts).total_seconds()
                if _quote_valid(quotes.get(symbol), now, 10**9)
                else None,
            }
            for symbol, rows in bars.items()
        }
        data_at = max((b.ts + _MINUTE for rows in bars.values() for b in rows), default=None)
    findings = quality_findings(
        coverage, quote_max_age_seconds=config.paper_broker.quote_max_age_seconds
    )
    return _store(
        session_factory,
        name,
        now,
        {
            "session_date": info.session_date.isoformat(),
            "window_start": isoformat_z(start),
            "window_end": isoformat_z(end),
            "coverage": coverage,
            "evidence": findings,
            "data_at": isoformat_z(data_at) if data_at else None,
            "conclusion": f"Data quality: {len(findings)} findings",
            "limitations": [
                "Coverage describes the configured provider, not consolidated market data."
            ],
        },
        reason="no_market_data" if data_at is None else None,
    )


def run_opportunities_agent(
    session_factory: sessionmaker[Session],
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    now: datetime,
) -> str:
    name = "opportunities"
    info = calendar.session_at(now)
    if info is None:
        return _store(session_factory, name, now, {}, reason="outside_session")
    end = now.replace(second=0, microsecond=0)
    minutes = int((end - info.open_at).total_seconds() // 60)
    if minutes < 30:
        return _store(session_factory, name, now, {}, reason="insufficient_session_history")
    candidates: list[ActivityCandidate] = []
    excluded: dict[str, str] = {}
    with session_factory() as session:
        bars, quotes = _market(session, config, provider_name, info.open_at, end, now)
        for symbol, rows in bars.items():
            quote = quotes.get(symbol)
            if len(rows) != minutes or any(b.quality_flags for b in rows):
                excluded[symbol] = "incomplete_or_flagged_bars"
                continue
            if not _quote_valid(quote, now, config.paper_broker.quote_max_age_seconds):
                excluded[symbol] = "missing_or_stale_quote"
                continue
            assert quote is not None  # noqa: S101 - checked above
            spread = (quote.ask - quote.bid) / ((quote.bid + quote.ask) / 2) * 10000
            if spread > config.risk.max_spread_bps or rows[0].open <= 0:
                excluded[symbol] = "invalid_price_or_spread"
                continue
            # Earlier complete five-minute buckets; the latest five minutes are held separate.
            earlier = rows[: minutes - 5]
            buckets = [
                sum((b.volume for b in earlier[i : i + 5]), _ZERO)
                for i in range(0, len(earlier) - 4, 5)
            ]
            average = sum(buckets, _ZERO) / len(buckets) if buckets else _ZERO
            if average <= 0:
                excluded[symbol] = "no_volume_baseline"
                continue
            candidates.append(
                ActivityCandidate(
                    symbol,
                    (rows[-1].close - rows[0].open) / rows[0].open,
                    sum((b.volume for b in rows[-5:]), _ZERO) / average,
                    spread,
                    rows[-1].ts + _MINUTE,
                )
            )
    ranked = rank_activity(candidates)
    symbols = ", ".join(str(r["symbol"]) for r in ranked) or "empty"
    return _store(
        session_factory,
        name,
        now,
        {
            "session_date": info.session_date.isoformat(),
            "items": ranked,
            "excluded": excluded,
            "data_at": isoformat_z(end) if ranked else None,
            "conclusion": f"Activity watchlist: {symbols}",
            "evidence": [
                f"{r['symbol']}: return {r['session_return_percent']}%, "
                f"relative volume {r['relative_volume']}"
                for r in ranked
            ],
            "limitations": [
                "Activity ranking only; score is not a probability of profit.",
                "Only the existing configured universe is considered.",
            ],
        },
        reason="no_eligible_symbols" if not ranked else None,
    )


def _execution_breakdown(session: Session, day: date) -> dict[str, Any]:
    found = _account(session)
    if found is None:
        return {"by_symbol": {}, "by_strategy": {}}
    start = datetime.combine(day, time.min, tzinfo=UTC)
    params = {"acct": found[0], "start": start, "end": start + timedelta(days=1)}
    symbols: dict[str, dict[str, Any]] = {}
    strategies: dict[str, dict[str, Any]] = {}

    def item(target: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        return target.setdefault(
            key,
            {
                "by_status": {},
                "fills": 0,
                "fees": _ZERO,
                "slippage_estimate": _ZERO,
                "attributed_net_pnl": _ZERO,
            },
        )

    for row in session.execute(
        text(
            "SELECT i.symbol, o.status, n.strategy_ids FROM orders o"
            " JOIN instruments i ON i.id=o.instrument_id"
            " JOIN order_intents n ON n.id=o.intent_id"
            " WHERE o.account_id=:acct AND o.created_at>=:start AND o.created_at<:end"
        ),
        params,
    ):
        targets = [item(symbols, row.symbol), *(item(strategies, s) for s in row.strategy_ids)]
        for target in targets:
            counts = target["by_status"]
            counts[row.status] = counts.get(row.status, 0) + 1
    for row in session.execute(
        text(
            "SELECT i.symbol, count(*) AS fills, sum(f.fees) AS fees,"
            " sum(f.slippage_estimate) AS slippage FROM fills f JOIN orders o ON o.id=f.order_id"
            " JOIN instruments i ON i.id=o.instrument_id"
            " WHERE o.account_id=:acct AND f.ts>=:start AND f.ts<:end GROUP BY i.symbol"
        ),
        params,
    ):
        item(symbols, row.symbol).update(
            fills=row.fills, fees=row.fees, slippage_estimate=row.slippage
        )
    for row in session.execute(
        text(
            "SELECT a.strategy_id, count(*) AS fills, sum(a.allocated_fees) AS fees,"
            " sum(coalesce(a.pnl,0)) AS pnl, sum(f.slippage_estimate * abs(a.allocated_qty)"
            " / nullif((SELECT sum(abs(x.allocated_qty)) FROM strategy_attribution x"
            " WHERE x.fill_id=f.id),0)) AS slippage"
            " FROM strategy_attribution a JOIN fills f ON f.id=a.fill_id"
            " JOIN orders o ON o.id=f.order_id"
            " WHERE o.account_id=:acct AND f.ts>=:start AND f.ts<:end GROUP BY a.strategy_id"
        ),
        params,
    ):
        item(strategies, row.strategy_id).update(
            fills=row.fills,
            fees=row.fees,
            slippage_estimate=row.slippage or _ZERO,
            attributed_net_pnl=row.pnl,
        )
    for groups in (symbols, strategies):
        for metrics in groups.values():
            for key in ("fees", "slippage_estimate", "attributed_net_pnl"):
                metrics[key] = str(Decimal(metrics[key]).quantize(Decimal("0.0001")))
    return {"by_symbol": symbols, "by_strategy": strategies}


def run_execution_quality_agent(
    session_factory: sessionmaker[Session],
    *,
    config: AppConfig,
    calendar: MarketCalendar,
    provider_name: str,
    session_date: date,
    now: datetime,
) -> str:
    info = calendar.session_on(session_date)
    if info is None or now < info.close_at:
        return _store(session_factory, "execution_quality", now, {}, reason="session_not_finished")
    with session_factory() as session:
        report = daily_report(session, session_date, config=config, provider=provider_name)
        breakdown = _execution_breakdown(session, session_date)
    if "error" in report:
        return _store(session_factory, "execution_quality", now, {}, reason=str(report["error"]))
    return _store(
        session_factory,
        "execution_quality",
        now,
        {
            "session_date": session_date.isoformat(),
            **breakdown,
            "orders": report["orders"],
            "costs": report["costs"],
            "risk_denials": report["risk_denials"],
            "strategies": report["strategies"],
            "data_at": isoformat_z(info.close_at),
            "conclusion": f"Execution review: {report['orders']['fills']} fills",
            "evidence": [
                f"Fees: {report['costs']['fees']} USD",
                f"Estimated slippage: {report['costs']['slippage_estimate']} USD",
            ],
            "limitations": [
                "Slippage is informational and already reflected in execution prices; "
                "it is not subtracted again from P&L.",
                "Shared orders appear under each participating strategy; "
                "allocated costs are split.",
            ],
        },
        reason="no_session_observations"
        if not report["ticks"] and not report["orders"]["total"]
        else None,
    )
