"""Alpaca Market Data adapter (free IEX feed). DATA ONLY.

This talks to ``data.alpaca.markets`` and nothing else: the constructor refuses any other host, so
a trading endpoint (``api.``/``paper-api.alpaca.markets``) can never be configured by mistake. It
has no order, account or position calls, and the repository holds no trading credentials.

Documented behaviour of the free plan: the IEX feed covers one venue, so bar volume is a small
fraction of consolidated volume (the ORB volume ratio is relative, so it is unaffected, but
absolute volume filters are). Quotes are fetched as the last quote per symbol-minute (one request
each; the free-plan rate limit applies, so large backfills are slow). Corporate actions are not
fetched by this adapter.
"""

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from trading.core.errors import TradingError
from trading.core.time import ensure_utc, isoformat_z
from trading.data.models import Bar, CorporateActionRecord, Quote
from trading.data.provider import DataFormatError
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
DATA_HOST = "data.alpaca.markets"
PROVIDER_NAME = "alpaca-iex"
PAGE_LIMIT = 10000
MAX_RETRIES = 3
MAX_RATE_LIMIT_RETRIES = 12
_FRACTION = re.compile(r"(\.\d{6})\d+")


class ProviderError(TradingError):
    """The data provider failed (network, rate limit, server error)."""


class ProviderAuthError(ProviderError):
    """The provider rejected our credentials."""


def _parse_ts(value: str) -> datetime:
    # Alpaca timestamps carry nanoseconds; Python keeps microseconds.
    return ensure_utc(datetime.fromisoformat(_FRACTION.sub(r"\1", value).replace("Z", "+00:00")))


class AlpacaDataProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        key_id: str,
        secret: str,
        *,
        base_url: str = f"https://{DATA_HOST}",
        feed: str = "iex",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        workers: int = 1,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname != DATA_HOST:
            raise ValueError(f"only https://{DATA_HOST} (market data) is allowed, got {base_url!r}")
        self._feed = feed
        self._workers = max(1, workers)
        self._sleep = sleep
        self._client = httpx.Client(
            base_url=base_url,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret},
            timeout=10.0,
            transport=transport,
        )

    @classmethod
    def from_files(cls, key_id_file: Path, secret_file: Path, **kwargs: Any) -> Self:
        return cls(key_id_file.read_text().strip(), secret_file.read_text().strip(), **kwargs)

    def __repr__(self) -> str:
        return f"AlpacaDataProvider(feed={self._feed!r})"  # never includes credentials

    def close(self) -> None:
        self._client.close()

    # ---- HTTP -----------------------------------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        last: str = ""
        failures = rate_limits = 0
        while True:
            wait = 0.0
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last = f"network error: {type(exc).__name__}"
                failures += 1
                wait = 0.5 * 2 ** (failures - 1)
            else:
                code = response.status_code
                if code in (401, 403):
                    raise ProviderAuthError(f"alpaca rejected the credentials (HTTP {code})")
                if code == 429:
                    last = "HTTP 429"
                    rate_limits += 1
                    retry_after = response.headers.get("Retry-After", "")
                    hinted = (
                        float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.0
                    )
                    wait = max(hinted, min(2.0 * 2 ** (rate_limits - 1), 30.0))
                elif code >= 500:
                    last = f"HTTP {code}"
                    failures += 1
                    wait = 0.5 * 2 ** (failures - 1)
                elif code != 200:
                    raise ProviderError(f"alpaca request failed (HTTP {code}) for {path}")
                else:
                    decoded: dict[str, Any] = json.loads(response.content, parse_float=Decimal)
                    return decoded
            if failures > MAX_RETRIES or rate_limits > MAX_RATE_LIMIT_RETRIES:
                attempts = failures + rate_limits
                raise ProviderError(
                    f"alpaca unavailable after {attempts} attempts ({last}) for {path}"
                )
            self._sleep(wait)

    def _pages(
        self, path: str, key: str, symbols: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "start": isoformat_z(start),
            "end": isoformat_z(end),
            "feed": self._feed,
            "limit": PAGE_LIMIT,
            "sort": "asc",
        }
        if key == "bars":
            params.update(timeframe="1Min", adjustment="raw")
        while True:
            body = self._get(path, params)
            for symbol, records in (body.get(key) or {}).items():
                for record in records:
                    yield symbol, record
            token = body.get("next_page_token")
            if not token:
                return
            params["page_token"] = token

    # ---- MarketDataProvider ---------------------------------------------------------------------

    def fetch_bars(self, symbols: Sequence[str], start: datetime, end: datetime) -> Iterator[Bar]:
        for symbol, r in self._pages("/v2/stocks/bars", "bars", symbols, start, end):
            try:
                ts = _parse_ts(r["t"])
                if not start <= ts < end:
                    continue
                yield Bar(
                    symbol=symbol,
                    interval="1m",
                    ts=ts,
                    open=Decimal(r["o"]),
                    high=Decimal(r["h"]),
                    low=Decimal(r["l"]),
                    close=Decimal(r["c"]),
                    volume=Decimal(r["v"]),
                    provider=self.name,
                    source_seq=None,
                )
            except (KeyError, ValueError, ArithmeticError, ValidationError) as exc:
                raise DataFormatError(f"alpaca bar {symbol} {r.get('t')!r}: {exc!r}") from exc

    def fetch_quotes(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> Iterator[Quote]:
        """The LAST quote of each symbol in each minute of ``[start, end)``.

        Raw IEX quotes are ~10,000 ticks a minute for SPY alone, so one small request per
        symbol-minute (``limit=1``, newest first) is used instead of downloading ticks.
        """
        tasks: list[tuple[str, datetime, datetime]] = []
        minute = start.replace(second=0, microsecond=0)
        while minute < end:
            window_end = min(minute + timedelta(minutes=1), end)
            tasks.extend((symbol, max(minute, start), window_end) for symbol in symbols)
            minute += timedelta(minutes=1)
        if self._workers == 1:
            for task in tasks:
                yield from self._last_quote(*task)
            return
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            for quotes in pool.map(lambda t: list(self._last_quote(*t)), tasks):
                yield from quotes

    def _last_quote(self, symbol: str, start: datetime, end: datetime) -> Iterator[Quote]:
        body = self._get(
            "/v2/stocks/quotes",
            {
                "symbols": symbol,
                "start": isoformat_z(start),
                "end": isoformat_z(end),
                "feed": self._feed,
                "limit": 1,
                "sort": "desc",
            },
        )
        for r in (body.get("quotes") or {}).get(symbol, []):
            yield self._quote(symbol, r)

    def _quote(self, symbol: str, r: Mapping[str, Any]) -> Quote:
        try:
            return Quote(
                symbol=symbol,
                ts=_parse_ts(r["t"]),
                bid=Decimal(r["bp"]),
                ask=Decimal(r["ap"]),
                bid_size=Decimal(r["bs"]) if r.get("bs") is not None else None,
                ask_size=Decimal(r["as"]) if r.get("as") is not None else None,
                provider=self.name,
            )
        except (KeyError, ValueError, ArithmeticError, ValidationError) as exc:
            raise DataFormatError(f"alpaca quote {symbol} {r.get('t')!r}: {exc!r}") from exc

    def fetch_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> Iterator[CorporateActionRecord]:
        _log.warning("alpaca_corporate_actions_unsupported")
        return iter(())
