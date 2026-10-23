"""Identifier helpers."""

from datetime import date
from uuid import UUID, uuid4


def new_id() -> UUID:
    return uuid4()


def make_client_order_id(strategy_key: str, session_date: date, seq: int) -> str:
    return f"paper-{strategy_key}-{session_date:%Y%m%d}-{seq:04d}"
