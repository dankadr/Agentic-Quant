from sqlalchemy.types import Float

import trading.db.models  # noqa: F401  (registers all tables on Base.metadata)
from trading.db.base import Base


def test_no_float_columns() -> None:
    offenders = [
        f"{table.name}.{col.name}"
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, Float)
    ]
    assert offenders == []


def test_models_registered() -> None:
    assert {"jobs", "leases", "outbox", "audit_events", "system_state"} <= set(Base.metadata.tables)
