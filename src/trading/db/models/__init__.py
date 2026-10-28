"""Importing this package registers every table on ``Base.metadata``."""

from trading.db.models import documents, market, ops, portfolio, research, trading, venue

__all__ = ["documents", "market", "ops", "portfolio", "research", "trading", "venue"]
