"""Idempotency-Key handling, unique per principal and route.

The key row is claimed first, in the same transaction as the action. A concurrent duplicate
blocks on the unique index until the first request commits, then replays its stored response.
Failed actions roll the claim back, so errors are never cached.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal
from trading.api.problems import ApiProblem
from trading.core.hashing import canonical_json, content_hash

MAX_KEY_LENGTH = 128


def run_idempotent(
    session: Session,
    *,
    principal: AuthPrincipal,
    route: str,
    key: str | None,
    body: BaseModel,
    action: Callable[[], tuple[int, dict[str, Any]]],
    now: datetime,
) -> JSONResponse:
    if key is None or key == "":
        raise ApiProblem(400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required")
    if len(key) > MAX_KEY_LENGTH:
        raise ApiProblem(
            422, "INVALID_IDEMPOTENCY_KEY", f"Idempotency-Key exceeds {MAX_KEY_LENGTH} characters"
        )
    request_hash = content_hash(body.model_dump())
    params = {"p": principal.id, "route": route, "key": key}
    claimed = session.execute(
        text(
            "INSERT INTO idempotency_keys (principal_id, route, key, request_hash, status_code,"
            " response_json, created_at) VALUES (:p, :route, :key, :h, 0, '{}'::jsonb, :now)"
            " ON CONFLICT DO NOTHING RETURNING 1"
        ),
        {**params, "h": request_hash, "now": now},
    ).first()
    if claimed is None:
        stored = session.execute(
            text(
                "SELECT request_hash, status_code, response_json FROM idempotency_keys"
                " WHERE principal_id = :p AND route = :route AND key = :key"
            ),
            params,
        ).one()
        if stored.request_hash != request_hash:
            raise ApiProblem(
                422,
                "IDEMPOTENCY_KEY_REUSED",
                "Idempotency-Key was already used with a different request body",
            )
        return JSONResponse(
            stored.response_json,
            status_code=stored.status_code,
            headers={"Idempotent-Replayed": "true"},
        )
    status, payload = action()
    session.execute(
        text(
            "UPDATE idempotency_keys SET status_code = :s, response_json = CAST(:r AS jsonb)"
            " WHERE principal_id = :p AND route = :route AND key = :key"
        ),
        {**params, "s": status, "r": canonical_json(payload)},
    )
    session.commit()  # commit before responding so the client can read its own write
    return JSONResponse(payload, status_code=status)
