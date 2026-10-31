"""Fetch a document from an approved public source, with every hop checked.

Only https URLs whose host is on the configured allowlist are fetched. Every redirect is followed
by hand, and each target gets the same checks: it must be https, on the allowlist, and resolve only
to public addresses. Responses are streamed and capped, so a large or endless body cannot exhaust
memory.

Each hop is dialled at the address that was checked (pinned), with the original host name kept for
the Host header and for TLS. A DNS answer that changes later cannot redirect the connection, and a
different server at the checked address cannot pass the certificate check for the host name.
"""

import ipaddress
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from trading.core.config import DocumentsConfig
from trading.core.errors import TradingError

MAX_REDIRECTS = 3
_TIMEOUT_SECONDS = 20.0
_OVERALL_SECONDS = 60.0  # the whole fetch, across every redirect

Resolver = Callable[[str], list[str]]


class FetchRefused(TradingError):
    """A URL or a response was refused. ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Fetched:
    data: bytes
    content_type: str
    final_url: str


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def check_url(url: str, cfg: DocumentsConfig) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchRefused("NOT_HTTPS")
    if parts.username or parts.password:
        raise FetchRefused("CREDENTIALS_IN_URL")
    if parts.port not in (None, 443):
        raise FetchRefused("NON_STANDARD_PORT")
    host = (parts.hostname or "").lower()
    if not host or host not in {h.lower() for h in cfg.allowed_hosts}:
        raise FetchRefused("HOST_NOT_ALLOWED")
    return host


def check_addresses(addresses: list[str]) -> None:
    if not addresses:
        raise FetchRefused("NO_ADDRESS")
    for raw in addresses:
        if not ipaddress.ip_address(raw).is_global:
            raise FetchRefused("PRIVATE_ADDRESS")


@dataclass(frozen=True)
class _Pinned:
    url: httpx.URL
    headers: dict[str, str]
    extensions: dict[str, str]


def _pin(url: str, address: str, host: str) -> _Pinned:
    """Dial the address that was checked, while keeping the host name for Host and for TLS.

    The certificate is still verified against ``host``, so a different server at the checked address
    cannot answer for it.
    """
    return _Pinned(
        url=httpx.URL(url).copy_with(host=address),
        headers={"Host": host},
        extensions={"sni_hostname": host},
    )


def fetch(
    url: str,
    cfg: DocumentsConfig,
    *,
    client: httpx.Client,
    resolver: Resolver = system_resolver,
) -> Fetched:
    current = url
    started = time.monotonic()
    for _hop in range(MAX_REDIRECTS + 1):
        host = check_url(current, cfg)
        addresses = resolver(host)
        check_addresses(addresses)
        pinned = _pin(current, addresses[0], host)
        with client.stream(
            "GET",
            pinned.url,
            headers=pinned.headers,
            extensions=pinned.extensions,
            follow_redirects=False,
            timeout=_TIMEOUT_SECONDS,
        ) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                location = r.headers.get("location")
                if not location:
                    raise FetchRefused("REDIRECT_WITHOUT_LOCATION")
                current = urljoin(current, location)
                continue
            if r.status_code != 200:
                raise FetchRefused(f"HTTP_{r.status_code}")
            declared = int(r.headers.get("content-length") or 0)
            if declared > cfg.max_bytes:
                raise FetchRefused("TOO_LARGE")
            body = bytearray()
            for chunk in r.iter_bytes():
                if time.monotonic() - started > _OVERALL_SECONDS:
                    raise FetchRefused("TIMEOUT")  # an overall deadline, not a per-read one
                body.extend(chunk)
                if len(body) > cfg.max_bytes:
                    raise FetchRefused("TOO_LARGE")
            content_type = r.headers.get("content-type", "").split(";")[0].strip().lower()
            return Fetched(bytes(body), content_type, current)
    raise FetchRefused("TOO_MANY_REDIRECTS")
