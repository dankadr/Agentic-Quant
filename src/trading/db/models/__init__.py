"""Importing this package registers every table on ``Base.metadata``."""

from trading.db.models import ops

__all__ = ["ops"]
