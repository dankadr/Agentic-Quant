import httpx
import pytest

from trading.core.config import DocumentsConfig
from trading.documents.fetch import FetchRefused, check_addresses, check_url, fetch

CFG = DocumentsConfig(allowed_hosts=("filings.example",), max_bytes=1000)
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 - a public address
PRIVATE = lambda host: ["10.0.0.5"]  # noqa: E731 - an internal address


def _refused(reason: str):  # type: ignore[no-untyped-def]
    return pytest.raises(FetchRefused, match=reason)


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://filings.example/a.pdf", "NOT_HTTPS"),
        ("https://other.example/a.pdf", "HOST_NOT_ALLOWED"),
        ("https://user:pw@filings.example/a.pdf", "CREDENTIALS_IN_URL"),
        ("https://filings.example:8443/a.pdf", "NON_STANDARD_PORT"),
        ("https://localhost/a.pdf", "HOST_NOT_ALLOWED"),
    ],
)
def test_urls_outside_the_allowlist_are_refused(url: str, reason: str) -> None:
    with _refused(reason):
        check_url(url, CFG)


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "192.168.1.1"]
)
def test_internal_addresses_are_refused(address: str) -> None:
    with _refused("PRIVATE_ADDRESS"):
        check_addresses([address])


def test_a_public_address_is_accepted() -> None:
    check_addresses(["93.184.216.34"])


def test_a_redirect_into_the_internal_network_is_refused() -> None:
    """Each hop is resolved on its own: a public first host cannot launder a private second one."""
    cfg = DocumentsConfig(allowed_hosts=("filings.example", "mirror.example"), max_bytes=1000)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://mirror.example/internal"})

    def resolver(host: str) -> list[str]:
        return ["10.0.0.5"] if host == "mirror.example" else ["93.184.216.34"]

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with _refused("PRIVATE_ADDRESS"):
        fetch("https://filings.example/a.pdf", cfg, client=client, resolver=resolver)


def test_a_redirect_to_an_unlisted_host_is_refused() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with _refused("HOST_NOT_ALLOWED"):
        fetch("https://filings.example/a.pdf", CFG, client=client, resolver=PUBLIC)


def test_an_oversized_body_is_refused_while_streaming() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with _refused("TOO_LARGE"):
        fetch("https://filings.example/a.txt", CFG, client=client, resolver=PUBLIC)


def test_a_redirect_loop_is_bounded() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://filings.example/again"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with _refused("TOO_MANY_REDIRECTS"):
        fetch("https://filings.example/a.pdf", CFG, client=client, resolver=PUBLIC)
