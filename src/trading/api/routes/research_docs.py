"""Research documents: operator intake and viewer search, with citations resolved to retained text.

Responses carry text and metadata only. Nothing here renders stored HTML, so a hostile document
cannot run script in an operator's browser.
"""

import base64
import binascii
import hashlib
import json
from datetime import timedelta
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from trading.api.auth import AuthPrincipal, require_operator, require_viewer
from trading.api.deps import get_clock, get_session
from trading.api.problems import ApiProblem
from trading.core.clock import Clock
from trading.core.time import UtcDatetime
from trading.db.models.documents import (
    DocumentVersionRow,
    ResearchDocumentRow,
    ResearchFactRow,
    ResearchReportRow,
)
from trading.documents.blobs import BlobStore
from trading.documents.fetch import FetchRefused, check_url
from trading.documents.ingest import CATEGORIES, DocumentInput, DocumentRejected, ingest
from trading.documents.search import CitationNotFound, resolve_citation, search
from trading.jobs.queue import enqueue
from trading.reports.analyze import enqueue_analysis

router = APIRouter(prefix="/v1/research", tags=["research"])


class DocumentUpload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    category: str
    company: str | None = Field(default=None, max_length=12)
    source: str = Field(min_length=1, max_length=200)
    content_type: str
    content_base64: str
    published_at: UtcDatetime | None = None
    reporting_period: str | None = Field(default=None, max_length=40)


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(min_length=1, max_length=300)
    category: str
    company: str | None = Field(default=None, max_length=12)
    source: str = Field(min_length=1, max_length=200)
    published_at: UtcDatetime | None = None
    reporting_period: str | None = Field(default=None, max_length=40)


def _blobs(request: Request) -> BlobStore:
    blobs: BlobStore = request.app.state.blobs
    return blobs


def _rejected(reason: str) -> ApiProblem:
    status = 413 if reason == "TOO_LARGE" else 422
    return ApiProblem(status, reason, "Document refused", detail=reason)


@router.post("/documents", status_code=201)
def upload_document(
    body: DocumentUpload,
    request: Request,
    _: AuthPrincipal = Depends(require_operator),
    clock: Clock = Depends(get_clock),
) -> dict[str, Any]:
    cfg = request.app.state.config.documents
    if body.category not in CATEGORIES:
        raise _rejected("UNKNOWN_CATEGORY")
    # Refuse oversized input before decoding it: base64 is 4 characters per 3 bytes.
    if len(body.content_base64) > cfg.max_bytes * 4 // 3 + 4:
        raise _rejected("TOO_LARGE")
    try:
        data = base64.b64decode(body.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiProblem(422, "INVALID_BASE64", "Document refused") from exc
    try:
        result = ingest(
            request.app.state.session_factory,
            _blobs(request),
            cfg,
            DocumentInput(
                title=body.title,
                category=body.category,
                company=body.company.upper() if body.company else None,
                source=body.source,
                content_type=body.content_type,
                data=data,
                origin="UPLOAD",
                source_url=None,
                published_at=body.published_at,
                reporting_period=body.reporting_period,
            ),
            now=clock.now(),
        )
    except DocumentRejected as exc:
        raise _rejected(exc.reason) from exc
    if result.extraction_status == "EXTRACTED":
        with request.app.state.session_factory.begin() as session:
            enqueue_analysis(
                session,
                version_id=result.version_id,
                model=request.app.state.config.agents.llm.model,
                now=clock.now(),
            )
    return {
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
        "version_no": result.version_no,
        "sha256": result.sha256,
        "extraction_status": result.extraction_status,
        "deduplicated": result.deduplicated,
    }


@router.post("/import-url", status_code=202)
def import_url(
    body: ImportRequest,
    request: Request,
    _: AuthPrincipal = Depends(require_operator),
    clock: Clock = Depends(get_clock),
) -> dict[str, str]:
    """Queue a fetch from an approved source. The allowlist is checked now, before queueing."""
    cfg = request.app.state.config.documents
    if body.category not in CATEGORIES:
        raise _rejected("UNKNOWN_CATEGORY")
    try:
        check_url(body.url, cfg)
    except FetchRefused as exc:
        raise ApiProblem(422, exc.reason, "URL refused", detail=exc.reason) from exc
    payload: dict[str, Any] = {
        "url": body.url,
        "title": body.title,
        "category": body.category,
        "company": body.company.upper() if body.company else None,
        "source": body.source,
        "published_at": body.published_at.isoformat() if body.published_at else None,
        "reporting_period": body.reporting_period,
    }
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with request.app.state.session_factory.begin() as session:
        job_id = enqueue(
            session,
            kind="document_import",
            payload=payload,
            scheduled_at=clock.now(),
            dedupe_key=f"document_import:{key}",
        )
    return {"job_id": str(job_id)}


@router.get("/documents")
def list_documents(
    request: Request,
    q: str | None = Query(default=None, min_length=2, max_length=200),
    company: str | None = Query(default=None, max_length=12),
    category: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=20, ge=1, le=100),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Full-text search when ``q`` is given; otherwise the newest documents."""
    if q is not None:
        hits = search(
            session,
            query=q,
            company=company,
            category=category,
            limit=limit,
        )
        return {
            "items": [
                {
                    "document_id": str(h.document_id),
                    "version_id": str(h.version_id),
                    "version_no": h.version_no,
                    "title": h.title,
                    "category": h.category,
                    "company": h.company,
                    "sha256": h.sha256,
                    "published_at": h.published_at,
                    "retrieved_at": h.retrieved_at,
                    "extraction_status": h.extraction_status,
                    "matched_pages": h.pages,
                }
                for h in hits
            ]
        }
    stmt = select(ResearchDocumentRow).order_by(ResearchDocumentRow.created_at.desc()).limit(limit)
    if company is not None:
        stmt = stmt.where(ResearchDocumentRow.company == company.upper())
    if category is not None:
        stmt = stmt.where(ResearchDocumentRow.category == category)
    return {
        "items": [
            {
                "document_id": str(d.id),
                "title": d.title,
                "category": d.category,
                "company": d.company,
            }
            for d in session.scalars(stmt)
        ]
    }


@router.get("/documents/{document_id}")
def document_versions(
    document_id: UUID,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    doc = session.get(ResearchDocumentRow, document_id)
    if doc is None:
        raise ApiProblem(404, "NOT_FOUND", "No such document")
    versions = session.scalars(
        select(DocumentVersionRow)
        .where(DocumentVersionRow.document_id == document_id)
        .order_by(DocumentVersionRow.version_no)
    ).all()
    return {
        "document_id": str(doc.id),
        "title": doc.title,
        "category": doc.category,
        "company": doc.company,
        "versions": [
            {
                "version_id": str(v.id),
                "version_no": v.version_no,
                "sha256": v.sha256,
                "origin": v.origin,
                "source": v.source,
                "source_url": v.source_url,
                # Two clocks, never merged: the source's publication claim and our retrieval.
                "published_at": v.published_at,
                "retrieved_at": v.retrieved_at,
                "reporting_period": v.reporting_period,
                "extraction_status": v.extraction_status,
                "extraction_error": v.extraction_error,
                "amendment_of": str(v.amendment_of) if v.amendment_of else None,
            }
            for v in versions
        ],
    }


@router.get("/citations")
def citation(
    version_id: UUID,
    page: int = Query(ge=1),
    start: int = Query(ge=0),
    end: int = Query(gt=0),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    try:
        quote = resolve_citation(session, version_id=version_id, page=page, start=start, end=end)
    except CitationNotFound as exc:
        raise ApiProblem(
            404, "CITATION_NOT_FOUND", "Citation does not resolve", detail=str(exc)
        ) from exc
    return {
        "version_id": str(quote.version_id),
        "sha256": quote.sha256,
        "page": quote.page,
        "start": quote.start,
        "end": quote.end,
        "text": quote.text,
    }


@router.get("/reports")
def list_reports(
    request: Request,
    company: str | None = Query(default=None, max_length=12),
    category: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Analysis outcomes, including refusals and deferrals, newest first."""
    stmt = select(ResearchReportRow).order_by(ResearchReportRow.created_at.desc()).limit(limit)
    if company is not None:
        stmt = stmt.where(ResearchReportRow.company == company.upper())
    if category is not None:
        stmt = stmt.where(ResearchReportRow.category == category)
    if status is not None:
        stmt = stmt.where(ResearchReportRow.status == status)
    return {
        "items": [
            {
                "report_id": str(r.id),
                "version_id": str(r.version_id),
                "category": r.category,
                "company": r.company,
                "status": r.status,
                "reason": r.reason,
                "model": r.model,
                "prompt_version": r.prompt_version,
                "created_at": r.created_at,
                # Usable for a decision only from this moment on.
                "available_at": r.available_at,
            }
            for r in session.scalars(stmt)
        ]
    }


@router.get("/reports/{report_id}")
def report_detail(
    report_id: UUID,
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """The findings with their evidence. Every fact and sourced claim carries a citation."""
    report = session.get(ResearchReportRow, report_id)
    if report is None:
        raise ApiProblem(404, "NOT_FOUND", "No such report")
    facts = session.scalars(
        select(ResearchFactRow).where(ResearchFactRow.report_id == report_id)
    ).all()
    return {
        "report_id": str(report.id),
        "version_id": str(report.version_id),
        "status": report.status,
        "reason": report.reason,
        "available_at": report.available_at,
        "findings": report.findings_json,
        "facts": [
            {
                "key": f.fact_key,
                "value": format(f.value.normalize(), "f"),  # 4.200000 -> 4.2
                "unit": f.unit,
                "period": f.period,
                "basis": f.basis,
                "citation": {"page": f.page, "start": f.span_start, "end": f.span_end},
            }
            for f in facts
        ],
    }


@router.get("/digest")
def digest(
    request: Request,
    as_of: UtcDatetime,
    kind: Literal["PREMARKET", "EOD"] = Query(default="PREMARKET"),
    _: AuthPrincipal = Depends(require_viewer),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Completed research that was AVAILABLE by ``as_of``, grouped by category, with its limits."""
    window_start = as_of - timedelta(hours=24)
    rows = session.scalars(
        select(ResearchReportRow).where(
            ResearchReportRow.status == "COMPLETED",
            ResearchReportRow.available_at <= as_of,
            ResearchReportRow.available_at > window_start,
        )
    ).all()
    by_category: dict[str, list[dict[str, Any]]] = {}
    for report in rows:
        findings: dict[str, Any] = report.findings_json or {}
        by_category.setdefault(report.category, []).append(
            {
                "report_id": str(report.id),
                "company": report.company,
                "fact_count": len(findings.get("facts", [])),
                "limitations": findings.get("limitations", []),
                "missing": findings.get("missing", []),
            }
        )
    return {"kind": kind, "as_of": as_of, "categories": by_category}
