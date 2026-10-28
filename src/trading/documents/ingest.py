"""Ingest a source document as an immutable version, exactly once per content.

Rules this module enforces:
- Duplicate content (same SHA-256) returns the existing version; it is never re-extracted.
- A changed document with the same logical identity is a NEW version that points at the one it
  amends. Revisions and amendments stay distinguishable; nothing is overwritten.
- ``published_at`` is the source's claim. ``retrieved_at`` is when this system obtained the bytes.
  Neither is ever back-filled or guessed.
- Parsing happens outside any database transaction, in an isolated process.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from trading.core.config import DocumentsConfig
from trading.core.errors import TradingError
from trading.db.models.documents import DocumentPageRow, DocumentVersionRow, ResearchDocumentRow
from trading.documents.blobs import BlobStore
from trading.documents.extract import extract
from trading.documents.sniff import ALLOWED_TYPES, sniff

CATEGORIES = frozenset(
    {"FINANCIAL_STATEMENT", "ANALYST_REPORT", "MACRO_RELEASE", "MANAGEMENT_CALL"}
)
ACCESS = frozenset({"PUBLIC", "RESTRICTED"})


class DocumentRejected(TradingError):
    """The input is refused before anything is stored. ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DocumentInput:
    title: str
    category: str
    company: str | None
    source: str
    content_type: str
    data: bytes
    origin: Literal["UPLOAD", "URL"]
    source_url: str | None
    published_at: datetime | None
    reporting_period: str | None
    access: str = "PUBLIC"


@dataclass(frozen=True)
class Ingested:
    document_id: UUID
    version_id: UUID
    version_no: int
    sha256: str
    extraction_status: str
    deduplicated: bool


def _logical_key(category: str, company: str | None, title: str) -> str:
    return f"{category}|{(company or '').strip().upper()}|{' '.join(title.split()).lower()}"


def _validate(doc: DocumentInput, cfg: DocumentsConfig) -> None:
    if doc.category not in CATEGORIES:
        raise DocumentRejected("UNKNOWN_CATEGORY")
    if doc.access not in ACCESS:
        raise DocumentRejected("UNKNOWN_ACCESS")
    if not doc.title.strip() or not doc.source.strip():
        raise DocumentRejected("MISSING_METADATA")
    if doc.content_type not in ALLOWED_TYPES:
        raise DocumentRejected("UNSUPPORTED_TYPE")
    if len(doc.data) > cfg.max_bytes:
        raise DocumentRejected("TOO_LARGE")
    if not doc.data:
        raise DocumentRejected("EMPTY")
    if sniff(doc.data) != doc.content_type:
        # The declared type must be the real one: a PDF labelled as HTML is refused, not guessed at.
        raise DocumentRejected("TYPE_MISMATCH")


def _existing(session: Session, sha256: str) -> DocumentVersionRow | None:
    return session.scalar(select(DocumentVersionRow).where(DocumentVersionRow.sha256 == sha256))


def _as_result(row: DocumentVersionRow, *, deduplicated: bool) -> Ingested:
    return Ingested(
        document_id=row.document_id,
        version_id=row.id,
        version_no=row.version_no,
        sha256=row.sha256,
        extraction_status=row.extraction_status,
        deduplicated=deduplicated,
    )


def ingest(
    session_factory: sessionmaker[Session],
    blobs: BlobStore,
    cfg: DocumentsConfig,
    doc: DocumentInput,
    *,
    now: datetime,
) -> Ingested:
    _validate(doc, cfg)
    sha = blobs.put(doc.data)
    with session_factory() as session:
        found = _existing(session, sha)
        if found is not None:
            return _as_result(found, deduplicated=True)

    result = extract(
        doc.data,
        doc.content_type,
        max_pages=cfg.max_pages,
        timeout_seconds=cfg.extract_timeout_seconds,
    )
    status = result.status if result.status in {"EXTRACTED", "OCR_REQUIRED"} else "FAILED"

    try:
        with session_factory.begin() as session:
            doc_row = session.scalar(
                select(ResearchDocumentRow).where(
                    ResearchDocumentRow.logical_key
                    == _logical_key(doc.category, doc.company, doc.title)
                )
            )
            if doc_row is None:
                doc_row = ResearchDocumentRow(
                    id=uuid4(),
                    logical_key=_logical_key(doc.category, doc.company, doc.title),
                    category=doc.category,
                    company=doc.company,
                    title=doc.title.strip(),
                    created_at=now,
                )
                session.add(doc_row)
                session.flush()
            latest = session.scalar(
                select(DocumentVersionRow)
                .where(DocumentVersionRow.document_id == doc_row.id)
                .order_by(DocumentVersionRow.version_no.desc())
                .limit(1)
            )
            version = DocumentVersionRow(
                id=uuid4(),
                document_id=doc_row.id,
                version_no=(latest.version_no + 1) if latest else 1,
                sha256=sha,
                byte_size=len(doc.data),
                content_type=doc.content_type,
                origin=doc.origin,
                source=doc.source.strip(),
                source_url=doc.source_url,
                published_at=doc.published_at,
                retrieved_at=now,
                reporting_period=doc.reporting_period,
                access=doc.access,
                extraction_status=status,
                extraction_error=result.error,
                amendment_of=latest.id if latest else None,
            )
            session.add(version)
            session.flush()
            if status in {"EXTRACTED", "OCR_REQUIRED"}:
                for number, text in enumerate(result.pages, start=1):
                    session.add(DocumentPageRow(version_id=version.id, page=number, text=text))
            session.flush()
            return _as_result(version, deduplicated=False)
    except IntegrityError:
        # Another writer stored the same content first. Their version is the one that counts.
        with session_factory() as session:
            found = _existing(session, sha)
            if found is None:
                raise
            return _as_result(found, deduplicated=True)
