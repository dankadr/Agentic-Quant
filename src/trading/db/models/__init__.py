"""Importing this package registers every table on ``Base.metadata``."""

from trading.db.models import (
    documents,
    market,
    ops,
    ops_alerts,
    portfolio,
    research,
    trading,
    venue,
)

__all__ = ["documents", "market", "ops", "ops_alerts", "portfolio", "research", "trading", "venue"]
