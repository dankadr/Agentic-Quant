"""Document intake against PostgreSQL: versions, dedupe, time rules, search, citations, immutability."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.clock import ManualClock
from trading.core.config import DocumentsConfig
from trading.documents.blobs import BlobStore
from trading.documents.ingest import DocumentInput, DocumentRejected, ingest
from trading.documents.search import CitationNotFound, resolve_citation, search

CFG = DocumentsConfig(max_bytes=100_000, max_pages=10, extract_timeout_seconds=30)
PUBLISHED = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
REPORT = (
    b"Quarterly results. Revenue rose 12 percent to 4.2 billion dollars. "
    b"Management raised full-year guidance."
)


def _doc(data: bytes = REPORT, **overrides: object) -> DocumentInput:
    base: dict[str, object] = dict(
        title="Q3 earnings release",
        category="FINANCIAL_STATEMENT",
        company="AAPL",
        source="issuer-ir",
        content_type="text/plain",
        data=data,
        origin="UPLOAD",
        source_url=None,
        published_at=PUBLISHED,
        reporting_period="2026Q3",
    )
    base.update(overrides)
    return DocumentInput(**base)  # type: ignore[arg-type]


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def test_duplicate_content_is_stored_and_extracted_once(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    first = ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    again = ingest(session_factory, blobs, CFG, _doc(), now=NOW + timedelta(hours=1))
    assert again.deduplicated and not first.deduplicated
    assert (again.version_id, again.sha256) == (first.version_id, first.sha256)
    db.rollback()
    assert db.execute(text("select count(*) from document_versions")).scalar_one() == 1
    assert db.execute(text("select count(*) from document_pages")).scalar_one() == 1


def test_an_amendment_is_a_new_version_that_points_at_the_one_it_amends(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    original = ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    amended_bytes = REPORT + b" Restated: revenue was 4.1 billion after review."
    amended = ingest(session_factory, blobs, CFG, _doc(amended_bytes), now=NOW)
    assert amended.document_id == original.document_id
    assert (amended.version_no, amended.deduplicated) == (2, False)
    db.rollback()
    link = db.execute(
        text("select amendment_of from document_versions where id = :v"),
        {"v": str(amended.version_id)},
    ).scalar_one()
    assert str(link) == str(original.version_id)


def test_published_and_retrieved_times_are_kept_apart(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    """An old report uploaded today keeps its old publication date and today's retrieval date."""
    ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    db.rollback()
    published, retrieved = db.execute(
        text("select published_at, retrieved_at from document_versions")
    ).one()
    assert published == PUBLISHED
    assert retrieved == NOW


def test_oversize_and_mismatched_content_is_refused_before_anything_is_stored(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    tiny = DocumentsConfig(max_bytes=10, max_pages=10, extract_timeout_seconds=30)
    with pytest.raises(DocumentRejected) as too_big:
        ingest(session_factory, blobs, tiny, _doc(), now=NOW)
    assert too_big.value.reason == "TOO_LARGE"
    with pytest.raises(DocumentRejected) as mismatch:
        ingest(session_factory, blobs, CFG, _doc(content_type="text/html"), now=NOW)
    assert mismatch.value.reason == "TYPE_MISMATCH"
    db.rollback()
    assert db.execute(text("select count(*) from document_versions")).scalar_one() == 0


def test_a_failed_extraction_is_kept_as_a_visible_version(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    broken = b"%PDF-1.4 garbage that is not a pdf"
    result = ingest(
        session_factory, blobs, CFG, _doc(broken, content_type="application/pdf"), now=NOW
    )
    assert result.extraction_status == "FAILED"
    db.rollback()
    error = db.execute(
        text("select extraction_error from document_versions where id = :v"),
        {"v": str(result.version_id)},
    ).scalar_one()
    assert error.startswith("PARSE_FAILED")


def test_search_finds_the_text_and_a_citation_resolves_to_the_exact_words(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    other = _doc(
        b"Unrelated macro commentary about inflation.",
        title="Other",
        company=None,
        category="MACRO_RELEASE",
        published_at=None,
    )
    ingest(session_factory, blobs, CFG, other, now=NOW)
    with session_factory() as s:
        hits = search(s, query="guidance", company="AAPL")
        assert [h.title for h in hits] == ["Q3 earnings release"]
        version_id = hits[0].version_id
    with session_factory() as s:
        quote = resolve_citation(s, version_id=version_id, page=1, start=0, end=17)
    assert quote.text == "Quarterly results"


def test_a_citation_outside_the_retained_text_is_refused(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    result = ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    with session_factory() as s, pytest.raises(CitationNotFound):
        resolve_citation(s, version_id=result.version_id, page=1, start=0, end=10_000)
    with session_factory() as s, pytest.raises(CitationNotFound):
        resolve_citation(s, version_id=result.version_id, page=9, start=0, end=5)


def test_stored_versions_and_pages_cannot_be_edited(
    db: Session, session_factory: sessionmaker[Session], blobs: BlobStore
) -> None:
    result = ingest(session_factory, blobs, CFG, _doc(), now=NOW)
    with pytest.raises(DBAPIError, match=r"forbid|not allowed|immutable"):
        with session_factory.begin() as s:
            s.execute(
                text("update document_pages set text = 'changed' where version_id = :v"),
                {"v": str(result.version_id)},
            )
    db.rollback()


def test_the_research_worker_imports_an_approved_url_end_to_end(
    session_factory: sessionmaker[Session], calendar, tmp_path: Path
) -> None:
    from uuid import uuid4

    import httpx

    from trading.core.config import Settings, load_config
    from trading.jobs.queue import Job
    from trading.services.research import ResearchContext, build_research_handlers

    cfg = load_config(Path("configs")).model_copy(
        update={"documents": DocumentsConfig(allowed_hosts=("filings.example",), max_bytes=100_000)}
    )

    def served(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=REPORT, headers={"content-type": "text/plain; charset=utf-8"}
        )

    ctx = ResearchContext(
        settings=Settings(),
        config=cfg,
        session_factory=session_factory,
        clock=ManualClock(NOW),
        calendar=calendar,
        provider_name="replay",
        blobs=BlobStore(tmp_path / "blobs"),
        http=httpx.Client(transport=httpx.MockTransport(served)),
        resolver=lambda host: ["93.184.216.34"],  # a public address
    )
    job = Job(
        id=uuid4(),
        kind="document_import",
        payload={
            "url": "https://filings.example/q3.txt",
            "title": "Q3 earnings release",
            "category": "FINANCIAL_STATEMENT",
            "company": "AAPL",
            "source": "issuer-ir",
            "published_at": PUBLISHED.isoformat(),
            "reporting_period": "2026Q3",
        },
        attempts=1,
        max_attempts=5,
    )
    build_research_handlers(ctx)["document_import"](job)

    with session_factory() as s:
        hits = search(s, query="guidance")
    assert len(hits) == 1
    assert hits[0].extraction_status == "EXTRACTED"
