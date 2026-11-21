"""Historical split adjustments for signed share attribution and closed trades."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import String, cast, literal, select, text
from sqlalchemy.orm import Session

from trading.core.errors import TradingError
from trading.data.calendar import NEW_YORK
from trading.db.models.market import CorporateAction, Instrument
from trading.db.models.ops import AuditEventRow

_MARKER = "corporate_action.applied"


def applied_splits(
    session: Session, account_id: UUID, as_of: datetime
) -> dict[str, list[tuple[date, Decimal]]]:
    rows = session.execute(
        select(
            Instrument.symbol,
            CorporateAction.ex_date,
            CorporateAction.ratio_to,
            CorporateAction.ratio_from,
        )
        .join(CorporateAction, CorporateAction.instrument_id == Instrument.id)
        .join(
            AuditEventRow,
            AuditEventRow.target == literal(f"{account_id}:") + cast(CorporateAction.id, String),
        )
        .where(
            AuditEventRow.action == _MARKER,
            AuditEventRow.result == "ok",
            AuditEventRow.ts <= as_of,
            CorporateAction.action_type == "SPLIT",
        )
        .order_by(CorporateAction.ex_date, CorporateAction.id)
    )
    splits: dict[str, list[tuple[date, Decimal]]] = {}
    for symbol, ex_date, numerator, denominator in rows:
        if not numerator or not denominator:
            raise TradingError("applied split missing its ratio")
        splits.setdefault(symbol, []).append((ex_date, numerator / denominator))
    return splits


def split_factor(
    splits: dict[str, list[tuple[date, Decimal]]], symbol: str, ts: datetime
) -> Decimal:
    factor = Decimal(1)
    for ex_date, ratio in splits.get(symbol, ()):
        if ts.astimezone(NEW_YORK).date() < ex_date:
            factor *= ratio
    return factor


def dividend_allocations(
    session: Session, account_id: UUID, end: datetime
) -> list[tuple[datetime, str, str, Decimal]]:
    rows = session.execute(
        text(
            "SELECT a.ts, i.symbol, d.key, d.value::numeric FROM audit_events a "
            "JOIN corporate_actions c ON a.target = :prefix || c.id::text "
            "JOIN instruments i ON i.id = c.instrument_id "
            "CROSS JOIN LATERAL jsonb_each_text(a.details_json->'dividend_pnl') d "
            "WHERE a.action='corporate_action.applied' AND c.action_type='CASH_DIVIDEND' "
            "AND a.ts < :end"
        ),
        {"prefix": f"{account_id}:", "end": end},
    )
    return [(ts, symbol, strategy, amount) for ts, symbol, strategy, amount in rows]
