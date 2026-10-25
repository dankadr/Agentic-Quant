import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from trading.data.alpaca_provider import (
    AlpacaDataProvider,
    ProviderAuthError,
    ProviderError,
)
from trading.data.provider import DataFormatError

D = Decimal
START = datetime(2026, 10, 19, 13, 30, tzinfo=UTC)
END = datetime(2026, 10, 19, 13, 33, tzinfo=UTC)
END_2M = datetime(2026, 10, 19, 13, 32, tzinfo=UTC)
KEY, SECRET = "PKTESTKEYID", "s3cr3t-value-that-must-never-leak"


def bar(t: str, c: str = "100.1", v: int = 1200) -> dict[str, object]:
    return {"t": t, "o": 100.0, "h": 100.25, "l": 99.9, "c": float(c), "v": v, "n": 10, "vw": 100.0}


def provider(handler, **kw) -> AlpacaDataProvider:  # type: ignore[no-untyped-def]
    return AlpacaDataProvider(
        KEY,
        SECRET,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kw,
    )


def respond(payload: object, status: int = 200) -> httpx.Response:
    # float-looking JSON numbers, exactly as the real API sends them
    return httpx.Response(status, content=json.dumps(payload).encode())


def test_bars_request_is_data_only_iex_and_authenticated() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return respond({"bars": {"SPY": [bar("2026-10-19T13:30:00Z")]}, "next_page_token": None})

    (b,) = provider(handler).fetch_bars(["SPY", "QQQ"], START, END)
    req = seen[0]
    assert req.url.host == "data.alpaca.markets"
    assert req.url.path == "/v2/stocks/bars"
    q = dict(req.url.params)
    assert (q["symbols"], q["timeframe"], q["feed"], q["adjustment"]) == (
        "SPY,QQQ",
        "1Min",
        "iex",
        "raw",
    )
    assert q["start"] == "2026-10-19T13:30:00Z"
    assert q["end"] == "2026-10-19T13:33:00Z"
    assert req.headers["APCA-API-KEY-ID"] == KEY
    assert req.headers["APCA-API-SECRET-KEY"] == SECRET
    assert (b.symbol, b.interval, b.ts, b.provider) == ("SPY", "1m", START, "alpaca-iex")


def test_prices_are_exact_decimals_never_floats() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return respond({"bars": {"SPY": [bar("2026-10-19T13:30:00Z", c="100.1")]}})

    (b,) = provider(handler).fetch_bars(["SPY"], START, END)
    assert b.close == D("100.1")
    assert b.high == D("100.25")
    assert all(isinstance(x, Decimal) for x in (b.open, b.high, b.low, b.close, b.volume))


def test_bars_follow_pagination_and_respect_the_half_open_range() -> None:
    pages = iter(
        [
            {
                "bars": {"SPY": [bar("2026-10-19T13:30:00Z"), bar("2026-10-19T13:31:00Z")]},
                "next_page_token": "p2",
            },
            {
                "bars": {"SPY": [bar("2026-10-19T13:32:00Z"), bar("2026-10-19T13:33:00Z")]},
                "next_page_token": None,
            },
        ]
    )
    tokens: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        tokens.append(req.url.params.get("page_token"))
        return respond(next(pages))

    out = list(provider(handler).fetch_bars(["SPY"], START, END))
    assert tokens == [None, "p2"]
    assert [b.ts.minute for b in out] == [30, 31, 32]  # 13:33 is the exclusive end


def test_quotes_are_the_last_quote_per_symbol_minute() -> None:
    seen: list[dict[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/v2/stocks/quotes"
        seen.append(dict(req.url.params))
        sym = req.url.params["symbols"]
        t = req.url.params["start"][:16] + ":59.123456789Z"
        return respond({"quotes": {sym: [{"t": t, "bp": 100.09, "bs": 2, "ap": 100.11, "as": 3}]}})

    out = list(provider(handler).fetch_quotes(["SPY", "QQQ"], START, END_2M))
    assert [(q.symbol, q.ts.minute) for q in out] == [
        ("SPY", 30),
        ("QQQ", 30),
        ("SPY", 31),
        ("QQQ", 31),
    ]
    assert all(p["limit"] == "1" and p["sort"] == "desc" and p["feed"] == "iex" for p in seen)
    assert (seen[0]["start"], seen[0]["end"]) == ("2026-10-19T13:30:00Z", "2026-10-19T13:31:00Z")
    q = out[0]
    assert (q.bid, q.ask, q.bid_size, q.ask_size, q.provider) == (
        D("100.09"),
        D("100.11"),
        D(2),
        D(3),
        "alpaca-iex",
    )
    assert q.ts == datetime(2026, 10, 19, 13, 30, 59, 123456, tzinfo=UTC)


def test_a_minute_without_quotes_yields_nothing() -> None:
    assert (
        list(provider(lambda r: respond({"quotes": {}})).fetch_quotes(["SPY"], START, END_2M)) == []
    )


def test_trading_hosts_and_plain_http_are_refused() -> None:
    for url in (
        "https://api.alpaca.markets",
        "https://paper-api.alpaca.markets",
        "http://data.alpaca.markets",
        "https://evil.example",
    ):
        with pytest.raises(ValueError, match="data"):
            AlpacaDataProvider(KEY, SECRET, base_url=url)


def test_auth_failure_is_reported_without_leaking_the_secret() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return respond({"message": "forbidden."}, 403)

    with pytest.raises(ProviderAuthError) as err:
        list(provider(handler).fetch_bars(["SPY"], START, END))
    assert SECRET not in str(err.value)
    assert KEY not in str(err.value)
    assert SECRET not in repr(provider(handler))


def test_rate_limits_and_server_errors_are_retried_then_surface() -> None:
    calls = {"n": 0}

    def flaky(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return respond({"message": "slow down"}, 429 if calls["n"] == 1 else 503)
        return respond({"bars": {"SPY": [bar("2026-10-19T13:30:00Z")]}})

    assert len(list(provider(flaky).fetch_bars(["SPY"], START, END))) == 1
    assert calls["n"] == 3

    dead_calls = {"n": 0}

    def dead(req: httpx.Request) -> httpx.Response:
        dead_calls["n"] += 1
        return respond({}, 500)

    with pytest.raises(ProviderError):
        list(provider(dead).fetch_bars(["SPY"], START, END))
    assert dead_calls["n"] == 4  # 1 try + 3 retries


def test_a_malformed_record_names_the_symbol_and_timestamp() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return respond({"bars": {"SPY": [{"t": "2026-10-19T13:30:00Z", "o": 1}]}})

    with pytest.raises(DataFormatError, match="SPY"):
        list(provider(handler).fetch_bars(["SPY"], START, END))


def test_corporate_actions_are_not_supported_and_say_so() -> None:
    p = provider(lambda r: respond({}))
    assert list(p.fetch_corporate_actions(["SPY"], date(2026, 10, 1), date(2026, 10, 30))) == []


def test_credentials_load_from_files(tmp_path: Path) -> None:
    (tmp_path / "id").write_text(KEY + "\n")
    (tmp_path / "sec").write_text(SECRET + "\n")
    p = AlpacaDataProvider.from_files(
        tmp_path / "id",
        tmp_path / "sec",
        transport=httpx.MockTransport(lambda r: respond({"bars": {}})),
    )
    assert list(p.fetch_bars(["SPY"], START, END)) == []
    assert p.name == "alpaca-iex"


def test_parallel_quote_fetching_returns_the_same_quotes_in_the_same_order() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        sym = req.url.params["symbols"]
        t = req.url.params["start"][:16] + ":59Z"
        return respond({"quotes": {sym: [{"t": t, "bp": 100.0, "ap": 100.02, "bs": 1, "as": 1}]}})

    symbols = ["SPY", "QQQ", "IWM"]
    sequential = list(provider(handler).fetch_quotes(symbols, START, END))
    parallel = list(provider(handler, workers=4).fetch_quotes(symbols, START, END))
    assert [(q.symbol, q.ts) for q in parallel] == [(q.symbol, q.ts) for q in sequential]
    assert len(parallel) == 9
