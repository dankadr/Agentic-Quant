import json
from datetime import UTC, datetime

import httpx
import pytest

from trading.data.alpaca_news import AlpacaNewsSource
from trading.data.alpaca_provider import ProviderAuthError

START = datetime(2026, 10, 24, 0, 0, tzinfo=UTC)
END = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)


def item(
    i: int, headline: str = "Apple beats earnings", symbols: list[str] | None = None
) -> dict[str, object]:
    return {"id": i, "headline": headline, "summary": "s", "created_at": "2026-10-24T12:00:00Z",
            "symbols": symbols or ["AAPL"], "source": "benzinga", "author": "x", "content": "<p>ignore</p>"}  # fmt: skip


def source(handler) -> AlpacaNewsSource:  # type: ignore[no-untyped-def]
    return AlpacaNewsSource(
        "PK", "SECRET", transport=httpx.MockTransport(handler), sleep=lambda _s: None
    )


def test_news_request_is_data_only_and_paginated() -> None:
    seen: list[httpx.Request] = []
    pages = iter(
        [
            {"news": [item(1), item(2)], "next_page_token": "t2"},
            {"news": [item(3)], "next_page_token": None},
        ]
    )

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=json.dumps(next(pages)).encode())

    out = source(handler).fetch(["AAPL", "NVDA"], START, END, limit=10)
    assert [n.id for n in out] == ["1", "2", "3"]
    assert seen[0].url.host == "data.alpaca.markets"
    assert seen[0].url.path == "/v1beta1/news"
    assert dict(seen[0].url.params)["symbols"] == "AAPL,NVDA"
    assert seen[1].url.params["page_token"] == "t2"
    assert seen[0].headers["APCA-API-KEY-ID"] == "PK"


def test_only_the_wanted_fields_are_kept_and_html_content_is_dropped() -> None:
    out = source(lambda r: httpx.Response(200, json={"news": [item(1)]})).fetch(
        ["AAPL"], START, END, limit=5
    )
    n = out[0]
    assert (n.headline, n.symbols, n.source) == ("Apple beats earnings", ("AAPL",), "benzinga")
    assert not hasattr(n, "content")


def test_the_limit_caps_the_total_returned() -> None:
    page = {"news": [item(i) for i in range(10)], "next_page_token": "more"}
    out = source(lambda r: httpx.Response(200, json=page)).fetch(["AAPL"], START, END, limit=4)
    assert len(out) == 4


def test_auth_errors_surface() -> None:
    with pytest.raises(ProviderAuthError):
        source(lambda r: httpx.Response(401, json={})).fetch(["AAPL"], START, END, limit=5)


def test_other_hosts_are_refused() -> None:
    with pytest.raises(ValueError, match="data"):
        AlpacaNewsSource("PK", "S", base_url="https://evil.example")
