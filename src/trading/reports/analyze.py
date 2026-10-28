"""Analyse one stored document version: once per content, model and prompt version.

Every outcome is recorded, including refusals and deferrals, so an operator can see why a document
has no report. Analysis never reads or changes trading state, and nothing here may touch an order.
"""

import json
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.llm import LlmError, LlmInvalidOutput
from trading.agents.macro import JsonLlm
from trading.core.clock import Clock
from trading.core.config import AppConfig
from trading.db.models.documents import (
    DocumentPageRow,
    DocumentVersionRow,
    ResearchDocumentRow,
    ResearchFactRow,
    ResearchReportRow,
)
from trading.jobs.queue import enqueue
from trading.reports.contracts import PROMPT_VERSION, ReportOutput, system_prompt
from trading.reports.verify import verify


@dataclass(frozen=True)
class Analysis:
    report_id: UUID
    status: str
    reason: str | None
    deduplicated: bool


@dataclass(frozen=True)
class _Source:
    version_id: UUID
    category: str
    company: str | None
    source: str
    pages: list[str]


def enqueue_analysis(session: Session, *, version_id: UUID, model: str, now: datetime) -> UUID:
    """Queue analysis of a version. The key is the content, model and prompt: one job each."""
    return enqueue(
        session,
        kind="research_analyze",
        payload={"version_id": str(version_id), "model": model, "prompt_version": PROMPT_VERSION},
        scheduled_at=now,
        dedupe_key=f"research_analyze:{version_id}:{model}:{PROMPT_VERSION}",
    )


def _spent_today(session: Session, now: datetime) -> int:
    start = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    spent = session.scalar(
        select(
            func.coalesce(
                func.sum(ResearchReportRow.prompt_tokens + ResearchReportRow.completion_tokens), 0
            )
        ).where(ResearchReportRow.created_at >= start)
    )
    return int(spent or 0)


def _load(session_factory: sessionmaker[Session], version_id: UUID) -> _Source:
    with session_factory() as session:
        version = session.get(DocumentVersionRow, version_id)
        if version is None:
            raise LookupError(f"document version {version_id} not found")
        if version.extraction_status != "EXTRACTED":
            raise LookupError(f"version {version_id} has no extracted text to analyse")
        document = session.get(ResearchDocumentRow, version.document_id)
        if document is None:  # the foreign key makes this impossible; fail loudly if it happens
            raise LookupError(f"document for version {version_id} not found")
        pages = list(
            session.scalars(
                select(DocumentPageRow.text)
                .where(DocumentPageRow.version_id == version_id)
                .order_by(DocumentPageRow.page)
            )
        )
        return _Source(
            version_id=version_id,
            category=document.category,
            company=document.company,
            source=version.source,
            pages=pages,
        )


def _record(
    session_factory: sessionmaker[Session],
    src: _Source,
    *,
    model: str,
    status: str,
    reason: str | None,
    now: datetime,
    available_at: datetime | None = None,
    tokens: tuple[int, int] | None = None,
    findings: dict[str, object] | None = None,
    facts: list[ResearchFactRow] | None = None,
) -> Analysis:
    report_id = uuid4()
    with session_factory.begin() as session:
        session.add(
            ResearchReportRow(
                id=report_id,
                version_id=src.version_id,
                category=src.category,
                company=src.company,
                model=model,
                prompt_version=PROMPT_VERSION,
                status=status,
                reason=reason,
                created_at=now,
                available_at=available_at,
                prompt_tokens=None if tokens is None else tokens[0],
                completion_tokens=None if tokens is None else tokens[1],
                findings_json=findings,
            )
        )
        session.flush()
        for fact in facts or []:
            fact.report_id = report_id
            session.add(fact)
    return Analysis(report_id, status, reason, deduplicated=False)


def analyze_version(
    session_factory: sessionmaker[Session],
    llm: JsonLlm,
    *,
    config: AppConfig,
    version_id: UUID,
    clock: Clock,
) -> Analysis:
    model = config.agents.llm.model
    now = clock.now()

    with session_factory() as session:
        prior = session.scalar(
            select(ResearchReportRow).where(
                ResearchReportRow.version_id == version_id,
                ResearchReportRow.model == model,
                ResearchReportRow.prompt_version == PROMPT_VERSION,
            )
        )
        if prior is not None:
            return Analysis(prior.id, prior.status, prior.reason, deduplicated=True)
        spent = _spent_today(session, now)
    src = _load(session_factory, version_id)

    def refused(status: str, reason: str) -> Analysis:
        return _record(session_factory, src, model=model, status=status, reason=reason, now=now)

    # Worst case for ONE call: the page text (about 4 characters per token) plus a full completion.
    # Reserved before the call, so a single call can never take the day over budget.
    prompt_bound = sum(len(p) for p in src.pages) // 4 + 1
    completion_bound = config.agents.llm.max_tokens
    if spent + prompt_bound + completion_bound > config.documents.daily_token_budget:
        return refused("DEFERRED", "BUDGET_EXHAUSTED")
    # Refuse rather than truncate: a silently shortened document would be analysed incompletely.
    if sum(len(p) for p in src.pages) > config.documents.max_input_chars:
        return refused("REJECTED_INVALID", "CONTEXT_TOO_LARGE")

    user = json.dumps(
        {
            "company": src.company,
            "source": src.source,
            "pages": [{"page": i + 1, "text": text} for i, text in enumerate(src.pages)],
        },
        sort_keys=True,
    )
    # Failed or refused calls still spent tokens we cannot read back. Charge the bound instead:
    # a rejected answer is followed by one repair call, so it is charged twice.
    try:
        result = llm.complete_json(system_prompt(src.category), user, ReportOutput)
    except LlmInvalidOutput as exc:
        return _charged(session_factory, src, model, "REJECTED_INVALID", str(exc), now,
                        prompt_bound, 2 * completion_bound)  # fmt: skip
    except LlmError as exc:
        return _charged(session_factory, src, model, "FAILED", str(exc), now,
                        prompt_bound, completion_bound)  # fmt: skip

    tokens = (result.prompt_tokens, result.completion_tokens)
    problems = verify(result.value, src.pages)
    if problems:
        return _record(
            session_factory,
            src,
            model=model,
            status="REJECTED_INVALID",
            reason="; ".join(problems[:5]),
            now=now,
            tokens=tokens,
            findings={"violations": problems},
        )

    output = result.value
    facts = [
        ResearchFactRow(
            id=uuid4(),
            version_id=src.version_id,
            company=src.company,
            fact_key=fact.key,
            # Verified by ``verify``: it parses and appears in the quote it cites.
            value=Decimal(fact.value.replace(",", "")),
            unit=fact.unit,
            period=fact.period,
            basis=fact.basis,
            page=fact.citation.page,
            span_start=fact.citation.start,
            span_end=fact.citation.end,
        )
        for fact in output.facts
    ]
    try:
        return _record(
            session_factory,
            src,
            model=model,
            status="COMPLETED",
            reason=None,
            now=now,
            available_at=clock.now(),
            tokens=tokens,
            findings=output.model_dump(mode="json"),
            facts=facts,
        )
    except SQLAlchemyError as exc:
        # The model was paid for, so the spend must still be on the record, as a failure.
        return _record(
            session_factory,
            src,
            model=model,
            status="FAILED",
            reason=f"RECORD_FAILED:{type(exc).__name__}",
            now=now,
            tokens=tokens,
        )


def _charged(
    session_factory: sessionmaker[Session],
    src: _Source,
    model: str,
    status: str,
    reason: str,
    now: datetime,
    prompt_bound: int,
    completion_bound: int,
) -> Analysis:
    """A refused or failed call: record the conservative token bound it may have used."""
    return _record(
        session_factory,
        src,
        model=model,
        status=status,
        reason=reason,
        now=now,
        tokens=(prompt_bound, completion_bound),
        findings={"token_usage": "UPPER_BOUND_ESTIMATE"},
    )
