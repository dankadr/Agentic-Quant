"""Dashboard sessions: an API key is exchanged once for a short-lived server-side session.

The browser holds only an opaque random token, in an HttpOnly cookie. The server stores a hash of
that token and a CSRF token that every state-changing form must echo back. No API key and no
credential is ever kept in the browser.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, Role
from trading.db.models.ops import Principal
from trading.db.models.ops_alerts import UiSessionRow

SESSION_LIFETIME = timedelta(minutes=30)


@dataclass(frozen=True)
class UiSession:
    principal: AuthPrincipal
    csrf_token: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def open_session(session: Session, *, principal_id: UUID, now: datetime) -> tuple[str, str]:
    """Create a session. Returns (cookie token, CSRF token); the cookie value is never stored."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    session.add(
        UiSessionRow(
            id=uuid4(),
            token_hash=_hash(token),
            principal_id=principal_id,
            csrf_token=csrf,
            created_at=now,
            expires_at=now + SESSION_LIFETIME,
        )
    )
    return token, csrf


def lookup(session: Session, token: str | None, *, now: datetime) -> UiSession | None:
    if not token:
        return None
    row = session.scalar(select(UiSessionRow).where(UiSessionRow.token_hash == _hash(token)))
    if row is None or row.revoked_at is not None or row.expires_at <= now:
        return None
    principal = session.get(Principal, row.principal_id)
    if principal is None or principal.disabled_at is not None:
        return None
    return UiSession(
        principal=AuthPrincipal(id=principal.id, name=principal.name, role=Role(principal.role)),
        csrf_token=row.csrf_token,
    )


def revoke(session: Session, token: str | None, *, now: datetime) -> None:
    if not token:
        return
    row = session.scalar(select(UiSessionRow).where(UiSessionRow.token_hash == _hash(token)))
    if row is not None and row.revoked_at is None:
        row.revoked_at = now


def csrf_ok(expected: str, supplied: str | None) -> bool:
    return supplied is not None and secrets.compare_digest(expected, supplied)
