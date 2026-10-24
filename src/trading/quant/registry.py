"""Strategy registry: the ``strategies`` table is the source of truth for live parameters."""

import json
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from trading.core.config import AppConfig
from trading.core.hashing import canonical_json
from trading.db.models.research import Strategy

_NOT_STRATEGIES = frozenset({"cash_reserve"})
INITIAL_VERSION = "1.0.0"


def seed_strategies(session: Session, config: AppConfig, *, now: datetime) -> None:
    """Insert missing strategies from config. Existing rows are NEVER overwritten: approved
    parameter changes live in the table and must survive a restart."""
    for name, cfg in config.strategies.items():
        if name in _NOT_STRATEGIES:
            continue
        session.execute(
            insert(Strategy)
            .values(
                id=name,
                name=name,
                version=INITIAL_VERSION,
                enabled=cfg.enabled,
                capital_budget=config.system.starting_cash_usd * cfg.allocation_pct,
                params_json=json.loads(canonical_json(dict(cfg.params))),
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
    session.flush()
