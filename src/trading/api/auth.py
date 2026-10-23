"""API-key authentication and role checks. Only a SHA-256 of each key is stored."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.deps import get_session
from trading.api.problems import ApiProblem
from trading.db.models.ops import Principal


class Role(StrEnum):
    VIEWER = "viewer"
    OPERATOR = "operator"


@dataclass(frozen=True)
class AuthPrincipal:
    id: UUID
    name: str
    role: Role


def generate_api_key() -> str:
    return "trk_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    # Keys are 256-bit random values, so a plain SHA-256 is sufficient (no password stretching).
    return hashlib.sha256(key.encode()).hexdigest()


def create_principal(
    session: Session, *, name: str, role: Role, now: datetime
) -> tuple[AuthPrincipal, str]:
    """Create a principal and return it with its plaintext key, which is never stored."""
    key = generate_api_key()
    row = Principal(name=name, role=role.value, api_key_hash=hash_api_key(key), created_at=now)
    session.add(row)
    session.flush()
    return AuthPrincipal(id=row.id, name=name, role=role), key


def _unauthenticated() -> ApiProblem:
    return ApiProblem(
        401,
        "UNAUTHENTICATED",
        "Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def authenticate(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> AuthPrincipal:
    token = _bearer_token(authorization)
    if token is None:
        raise _unauthenticated()
    row = session.execute(
        select(Principal).where(Principal.api_key_hash == hash_api_key(token))
    ).scalar_one_or_none()
    if row is None or row.disabled_at is not None:
        raise _unauthenticated()
    return AuthPrincipal(id=row.id, name=row.name, role=Role(row.role))


def require_viewer(principal: AuthPrincipal = Depends(authenticate)) -> AuthPrincipal:
    return principal


def require_operator(principal: AuthPrincipal = Depends(authenticate)) -> AuthPrincipal:
    if principal.role is not Role.OPERATOR:
        raise ApiProblem(403, "FORBIDDEN", "Operator role required")
    return principal
