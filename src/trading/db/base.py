"""Declarative base. Quantities/prices default to NUMERIC(20,8); currency columns use (20,4)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ARRAY, DateTime, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

CASH = Numeric(20, 4)
PRICE = Numeric(20, 8)


class Base(DeclarativeBase):
    type_annotation_map = {
        Decimal: PRICE,
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
        list[str]: ARRAY(Text),
        str: Text,
    }
