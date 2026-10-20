"""Canonical JSON and content hashes, stable across key order and Decimal formatting."""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import overload
from uuid import UUID

from trading.core.time import isoformat_z


def _default(o: object) -> str:
    if isinstance(o, Decimal):
        return format(o.normalize(), "f") if o != 0 else "0"
    if isinstance(o, datetime):
        return isoformat_z(o)
    if isinstance(o, (date, UUID)):
        return str(o)
    if isinstance(o, Enum):
        return str(o.value)
    raise TypeError(f"cannot canonicalize {type(o).__name__}")


def canonical_json(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_default)


@overload
def content_hash(obj: None) -> None: ...
@overload
def content_hash(obj: object) -> str: ...
def content_hash(obj: object | None) -> str | None:
    if obj is None:
        return None
    return hashlib.sha256(canonical_json(obj).encode()).hexdigest()
