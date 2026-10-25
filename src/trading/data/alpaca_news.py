"""Alpaca news (same data-only host and keys as market data). Bodies/HTML are never requested."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from trading.core.time import isoformat_z
from trading.data.alpaca_provider import AlpacaDataProvider, _parse_ts
from trading.data.news import NewsItem
from trading.data.provider import DataFormatError

PAGE_SIZE = 50


class AlpacaNewsSource(AlpacaDataProvider):
    def fetch(
        self, symbols: Sequence[str], start: datetime, end: datetime, *, limit: int
    ) -> list[NewsItem]:
        items: list[NewsItem] = []
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": isoformat_z(start),
            "end": isoformat_z(end),
            "include_content": "false",
        }
        while len(items) < limit:
            params["limit"] = min(PAGE_SIZE, limit - len(items))
            body = self._get("/v1beta1/news", params)
            for raw in body.get("news") or []:
                try:
                    items.append(
                        NewsItem(
                            id=str(raw["id"]),
                            headline=str(raw["headline"]),
                            summary=str(raw.get("summary") or ""),
                            created_at=_parse_ts(raw["created_at"]),
                            symbols=tuple(raw.get("symbols") or ()),
                            source=str(raw.get("source") or ""),
                        )
                    )
                except (KeyError, ValueError, ValidationError) as exc:
                    raise DataFormatError(f"alpaca news item {raw.get('id')!r}: {exc!r}") from exc
                if len(items) >= limit:
                    break
            token = body.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        return items
