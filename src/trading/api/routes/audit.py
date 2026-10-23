"""Read access to the append-only audit trail."""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_viewer
from trading.api.deps import get_session
from trading.api.pagination import decode_cursor, encode_cursor
from trading.core.time import UtcDatetime
from trading.monitoring.audit import list_audit

router = APIRouter(prefix="/v1/audit", tags=["audit"])


class AuditItem(BaseModel):
    id: str
    actor: str
    action: str
    target: str
    ts: UtcDatetime
    request_id: str | None
    before_hash: str | None
    after_hash: str | None
    result: str


class AuditPage(BaseModel):
    items: list[AuditItem]
    next_cursor: str | None


@router.get("", response_model=AuditPage)
def get_audit(
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> AuditPage:
    before = decode_cursor(cursor) if cursor else None
    rows = list_audit(session, limit=limit + 1, before=before)
    page = rows[:limit]
    return AuditPage(
        items=[
            AuditItem(
                id=str(r.id),
                actor=r.actor,
                action=r.action,
                target=r.target,
                ts=r.ts,
                request_id=r.request_id,
                before_hash=r.before_hash,
                after_hash=r.after_hash,
                result=r.result,
            )
            for r in page
        ],
        next_cursor=encode_cursor(page[-1].ts, page[-1].id) if len(rows) > limit else None,
    )
