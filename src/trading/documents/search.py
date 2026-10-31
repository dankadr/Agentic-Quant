"""Search stored documents, and resolve citations back to retained source text.

A citation is (version, page, start, end). It resolves only against the version's own stored text,
and the quote it returns is exactly that slice. A citation that points outside the text, or to a
version that does not exist, is refused rather than approximated.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from trading.core.errors import TradingError
from trading.db.models.documents import DocumentPageRow, DocumentVersionRow, ResearchDocumentRow


class CitationNotFound(TradingError):
    """The citation does not resolve to retained text."""


@dataclass(frozen=True)
class SearchHit:
    document_id: UUID
    version_id: UUID
    version_no: int
    title: str
    category: str
    company: str | None
    sha256: str
    published_at: object
    retrieved_at: object
    extraction_status: str
    pages: list[int]


@dataclass(frozen=True)
class Quote:
    version_id: UUID
    page: int
    start: int
    end: int
    text: str
    sha256: str


def search(
    session: Session,
    *,
    query: str,
    company: str | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[SearchHit]:
    """Full-text match over extracted pages. Returns the matching pages of each version."""
    stmt = (
        select(
            ResearchDocumentRow.id,
            DocumentVersionRow.id,
            DocumentVersionRow.version_no,
            ResearchDocumentRow.title,
            ResearchDocumentRow.category,
            ResearchDocumentRow.company,
            DocumentVersionRow.sha256,
            DocumentVersionRow.published_at,
            DocumentVersionRow.retrieved_at,
            DocumentVersionRow.extraction_status,
            func.array_agg(DocumentPageRow.page.distinct()),
        )
        .join(DocumentVersionRow, DocumentVersionRow.document_id == ResearchDocumentRow.id)
        .join(DocumentPageRow, DocumentPageRow.version_id == DocumentVersionRow.id)
        # Match the stored, indexed tsvector column so the GIN index is used.
        .where(text("document_pages.tsv @@ plainto_tsquery('english', :q)").bindparams(q=query))
        .group_by(ResearchDocumentRow.id, DocumentVersionRow.id)
        .order_by(DocumentVersionRow.retrieved_at.desc())
        .limit(limit)
    )
    if company is not None:
        stmt = stmt.where(ResearchDocumentRow.company == company.upper())
    if category is not None:
        stmt = stmt.where(ResearchDocumentRow.category == category)
    return [
        SearchHit(
            document_id=row[0],
            version_id=row[1],
            version_no=row[2],
            title=row[3],
            category=row[4],
            company=row[5],
            sha256=row[6],
            published_at=row[7],
            retrieved_at=row[8],
            extraction_status=row[9],
            pages=sorted(int(p) for p in row[10]),
        )
        for row in session.execute(stmt).all()
    ]


def resolve_citation(
    session: Session, *, version_id: UUID, page: int, start: int, end: int
) -> Quote:
    version = session.get(DocumentVersionRow, version_id)
    if version is None:
        raise CitationNotFound("no such document version")
    row = session.get(DocumentPageRow, (version_id, page))
    if row is None:
        raise CitationNotFound("no such page in this version")
    if not 0 <= start < end <= len(row.text):
        raise CitationNotFound("offsets fall outside the page text")
    return Quote(
        version_id=version_id,
        page=page,
        start=start,
        end=end,
        text=row.text[start:end],
        sha256=version.sha256,
    )
