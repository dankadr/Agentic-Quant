"""Consensus agent (shadow only): would "agreement before entering" have added value?

Deterministic, no model. For every entry the strategies made in a session it asks whether the other
sources agreed at that moment: the macro view was not defensive (HIGH_VOLATILITY / UNKNOWN or a
multiplier below 0.5) and no other strategy signalled the opposite side of the same symbol around
the same time. It then compares the P&L of the trades consensus would have allowed with all trades.
It has no execution authority: nothing here reaches the allocator, risk engine or broker.
"""

import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.macro import latest_macro_view
from trading.core.config import AppConfig
from trading.core.hashing import canonical_json
from trading.core.time import isoformat_z
from trading.db.models.research import AgentRun
from trading.monitoring.report import _account

AGENT_NAME = "consensus"
PROMPT_VERSION = "consensus-v1"
DEFENSIVE_REGIMES = frozenset({"HIGH_VOLATILITY", "UNKNOWN"})
MIN_MULTIPLIER = Decimal("0.5")
CONFLICT_BEFORE = timedelta(seconds=120)  # look back only: nothing after the entry is usable
_ZERO = Decimal(0)


def _entries(session: Session, account_id: Any, start: datetime, end: datetime) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT a.strategy_id, i.symbol,"
                " min(f.ts) FILTER (WHERE a.allocated_qty > 0) AS entry_ts,"
                " coalesce(sum(a.pnl), 0) AS pnl"
                " FROM strategy_attribution a JOIN fills f ON f.id = a.fill_id"
                " JOIN orders o ON o.id = f.order_id JOIN instruments i ON i.id = o.instrument_id"
                " WHERE o.account_id = :acct AND f.ts >= :s AND f.ts < :e"
                " AND a.strategy_id <> 'eod_flatten'"
                " GROUP BY a.strategy_id, i.symbol"
                " HAVING bool_or(a.allocated_qty > 0) ORDER BY entry_ts, a.strategy_id, i.symbol"
            ),
            {"acct": account_id, "s": start, "e": end},
        )
    )


def _conflict(session: Session, strategy: str, symbol: str, at: datetime) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT 1 FROM signals g JOIN instruments i ON i.id = g.instrument_id"
                " WHERE i.symbol = :sym AND g.strategy_id <> :strat AND g.side = 'SELL'"
                " AND g.strategy_id <> 'eod_flatten'"
                # Causal: a signal counts only if it existed at the entry decision time.
                " AND g.generated_at >= :a AND g.generated_at <= :at LIMIT 1"
            ),
            {"sym": symbol, "strat": strategy, "a": at - CONFLICT_BEFORE, "at": at},
        ).first()
    )


def run_consensus_agent(
    session_factory: sessionmaker[Session],
    *,
    config: AppConfig,
    session_date: date,
    now: datetime,
) -> str:
    start = datetime.combine(session_date, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    agreed = vetoed = conflicts = 0
    base = consensus = missed = _ZERO
    with session_factory() as session:
        found = _account(session)
        rows = _entries(session, found[0], start, end) if found else []
        for r in rows:
            view = latest_macro_view(session, r.entry_ts)
            defensive = view is not None and (
                view.regime in DEFENSIVE_REGIMES or view.risk_multiplier < MIN_MULTIPLIER
            )
            conflict = _conflict(session, r.strategy_id, r.symbol, r.entry_ts)
            conflicts += 1 if conflict else 0
            pnl = Decimal(r.pnl)
            base += pnl
            if defensive or conflict:
                vetoed += 1
                missed += pnl
            else:
                agreed += 1
                consensus += pnl
    result = {
        "agent_name": AGENT_NAME,
        "prompt_version": PROMPT_VERSION,
        "created_at": isoformat_z(now),
        "session_date": session_date.isoformat(),
        "entries": len(rows) if found else 0,
        "agreed": agreed,
        "vetoed": vetoed,
        "conflicts": conflicts,
        "base_pnl": base,
        "consensus_pnl": consensus,
        "missed_pnl": missed,
        "value_added": consensus - base,
        "verdict": "SHADOW_ONLY",
        "limitations": ["one session is far too small a sample to judge consensus"],
    }
    with session_factory.begin() as session:
        session.add(
            AgentRun(
                id=uuid4(),
                agent_name=AGENT_NAME,
                model="deterministic",
                prompt_version=PROMPT_VERSION,
                started_at=now,
                completed_at=now,
                status="COMPLETED",
                cost_usd=_ZERO,
                result_json=json.loads(canonical_json(result)),
            )
        )
    return "COMPLETED"
