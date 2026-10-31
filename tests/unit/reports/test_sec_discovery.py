import httpx
import pytest

from trading.core.config import DocumentsConfig
from trading.documents.fetch import FetchRefused
from trading.documents.sec import discover, parse_submissions

BODY = {
    "filings": {
        "recent": {
            "accessionNumber": ["0000320193-26-000045", "0000320193-26-000040", "0000320193-26-000001"],
            "form": ["10-Q", "S-8", "8-K"],
            "filingDate": ["2026-10-30", "2026-10-20", "2026-09-01"],
            "primaryDocument": ["aapl-10q.htm", "s8.htm", "aapl-8k.htm"],
        }
    }
}  # fmt: skip

CFG = DocumentsConfig(
    allowed_hosts=("data.sec.gov", "www.sec.gov"), sec_user_agent="Dan Test dan@example.com"
)


def test_only_analysable_forms_are_kept_with_their_archive_urls() -> None:
    filings = parse_submissions(BODY, cik=320193)
    assert [f.form for f in filings] == ["10-Q", "8-K"]
    assert filings[0].url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000045/aapl-10q.htm"
    )
    assert filings[0].accession == "000032019326000045"


def test_discovery_declares_its_identity_and_spaces_requests() -> None:
    seen: list[httpx.Request] = []
    waits: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=BODY)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    filings = discover(client, 320193, CFG, sleep=waits.append)
    assert len(filings) == 2
    assert seen[0].headers["user-agent"] == "Dan Test dan@example.com"
    assert waits == [1.0]


def test_no_identity_means_no_sec_access() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=BODY)))
    with pytest.raises(FetchRefused, match="NO_SEC_IDENTITY"):
        discover(client, 320193, DocumentsConfig(allowed_hosts=("data.sec.gov",)))


def test_malformed_submissions_are_refused() -> None:
    with pytest.raises(FetchRefused, match="MALFORMED"):
        parse_submissions({"filings": "nope"}, cik=1)
