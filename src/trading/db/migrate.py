"""Programmatic Alembic access. Run from the project root (or set TRADING_PROJECT_ROOT)."""

import os
from pathlib import Path

from alembic import command
from alembic.config import Config


def _config(url: str) -> Config:
    root = Path(os.environ.get("TRADING_PROJECT_ROOT", Path.cwd()))
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade to ``revision``; ``revision="base"`` downgrades everything."""
    cfg = _config(url)
    if revision == "base":
        command.downgrade(cfg, "base")
    else:
        command.upgrade(cfg, revision)
