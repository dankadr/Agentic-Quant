"""Discover official SEC filings for configured issuers, through EDGAR's submissions list.

SEC asks every automated client to declare who it is, so each request carries the configured user
agent, and requests are spaced out. Discovery only returns candidate URLs. Each filing is then
fetched by the same allowlisted, guarded fetch as any other document.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from trading.core.config import DocumentsConfig
from trading.documents.fetch import FetchRefused, check_url

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"
FORMS = frozenset({"10-K", "10-Q", "8-K"})
_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Filing:
    form: str
    accession: str  # 18-digit, no dashes: the archive folder name
    filing_date: str  # YYYY-MM-DD, as EDGAR states it
    primary_document: str
    url: str


def parse_submissions(body: dict[str, object], *, cik: int) -> list[Filing]:
    """The recent filings of one issuer that are of a form we analyse."""
    filings = body.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict):
        raise FetchRefused("MALFORMED_SUBMISSIONS")
    columns = [recent.get(name, []) for name in ("accessionNumber", "form", "filingDate")]
    documents = recent.get("primaryDocument", [])
    out: list[Filing] = []
    for accession_raw, form, filed, document in zip(*columns, documents, strict=False):
        if form not in FORMS or not document:
            continue
        accession = str(accession_raw).replace("-", "")
        out.append(
            Filing(
                form=str(form),
                accession=accession,
                filing_date=str(filed),
                primary_document=str(document),
                url=ARCHIVE_URL.format(cik=cik, accession=accession, document=document),
            )
        )
    return out


def discover(
    client: httpx.Client,
    cik: int,
    cfg: DocumentsConfig,
    *,
    min_interval_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Filing]:
    if not cfg.sec_user_agent:
        raise FetchRefused("NO_SEC_IDENTITY")
    url = SUBMISSIONS_URL.format(cik=cik)
    check_url(url, cfg)  # data.sec.gov must be on the allowlist like any other source
    sleep(min_interval_seconds)  # courtesy spacing; SEC rate-limits aggressive clients
    response = client.get(
        url,
        headers={"User-Agent": cfg.sec_user_agent},
        timeout=_TIMEOUT_SECONDS,
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise FetchRefused(f"HTTP_{response.status_code}")
    body: object = response.json()
    if not isinstance(body, dict):
        raise FetchRefused("MALFORMED_SUBMISSIONS")
    return parse_submissions(body, cik=cik)
