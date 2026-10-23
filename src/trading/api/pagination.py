"""Opaque keyset cursors."""

import base64
import binascii
import json
from datetime import datetime
from uuid import UUID

from trading.api.problems import ApiProblem
from trading.core.time import ensure_utc, isoformat_z


def encode_cursor(ts: datetime, id_: UUID) -> str:
    raw = json.dumps({"ts": isoformat_z(ts), "id": str(id_)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return ensure_utc(datetime.fromisoformat(data["ts"])), UUID(data["id"])
    except (binascii.Error, ValueError, KeyError, TypeError) as exc:
        raise ApiProblem(422, "INVALID_CURSOR", "Invalid cursor") from exc
