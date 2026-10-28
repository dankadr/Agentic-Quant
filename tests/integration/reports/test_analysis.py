"""Analysis outcomes against PostgreSQL, with a scripted model: verified, refused, deferred, once."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from trading.agents.llm import LlmError, LlmResult
from trading.core.clock import ManualClock
from trading.core.config import DocumentsConfig, load_config
from trading.documents.blobs import BlobStore
from trading.documents.ingest import DocumentInput, ingest
from trading.reports.analyze import analyze_version
from trading.reports.contracts import ReportOutput

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
TEXT = b"Revenue rose 12 percent to 4.2 billion dollars. Guidance was raised for the year."
CFG = load_config(Path("configs"))


class ScriptedLlm:
    def __init__(self, reply: dict[str, object] | Exception) -> None:
        self.reply = reply
        self.calls = 0

    def complete_json(self, system: str, user: str, schema: type[ReportOutput]) -> LlmResult:  # type: ignore[type-arg]
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        value = schema.model_validate(self.reply)
        return LlmResult(value=value, model="scripted", prompt_tokens=100, completion_tokens=40)


def _reply(quote: str = "4.2 billion") -> dict[str, object]:
    page = TEXT.decode()
    start = page.index(quote)
    end = start + len(quote)
    cite = {"page": 1, "start": start, "end": end, "quote": page[start:end]}
    return {
        "facts": [
            {
                "key": "revenue",
                "value": "4.2",
                "unit": "USD_MILLIONS",
                "period": "2026Q3",
                "basis": "UNSTATED",
                "citation": cite,
            }
        ],
        "claims": [{"kind": "SOURCE_FACT", "text": "Revenue rose.", "citations": [cite]}],
        "limitations": ["no segment detail"],
        "missing": ["prior-year comparison"],
    }


def _version(db_factory: sessionmaker[Session], blobs: BlobStore, data: bytes = TEXT) -> UUID:
    result = ingest(
        db_factory,
        blobs,
        DocumentsConfig(max_bytes=100_000, max_pages=10, extract_timeout_seconds=30),
        DocumentInput(
            title="Q3 release",
            category="FINANCIAL_STATEMENT",
            company="AAPL",
            source="ir",
            content_type="text/plain",
            data=data,
            origin="UPLOAD",
            source_url=None,
            published_at=None,
            reporting_period="2026Q3",
        ),
        now=NOW,
    )
    return result.version_id


def test_a_verified_report_becomes_available_with_its_facts(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    clock = ManualClock(NOW)
    out = analyze_version(
        session_factory, ScriptedLlm(_reply()), config=CFG, version_id=version, clock=clock
    )
    assert out.status == "COMPLETED"
    db.rollback()
    row = db.execute(
        text("select status, available_at, prompt_tokens from research_reports where id = :i"),
        {"i": str(out.report_id)},
    ).one()
    assert (row[0], row[1] is not None, row[2]) == ("COMPLETED", True, 100)
    assert db.execute(text("select count(*) from research_facts")).scalar_one() == 1


def test_an_unsupported_claim_rejects_the_whole_report(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    bad = _reply()
    bad["facts"][0]["value"] = "9.9"  # type: ignore[index]
    out = analyze_version(
        session_factory, ScriptedLlm(bad), config=CFG, version_id=version, clock=ManualClock(NOW)
    )
    assert (out.status, "does not appear" in (out.reason or "")) == ("REJECTED_INVALID", True)
    db.rollback()
    assert db.execute(text("select count(*) from research_facts")).scalar_one() == 0


def test_the_same_content_is_analysed_once_per_model_and_prompt(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    llm = ScriptedLlm(_reply())
    first = analyze_version(
        session_factory, llm, config=CFG, version_id=version, clock=ManualClock(NOW)
    )
    again = analyze_version(
        session_factory, llm, config=CFG, version_id=version, clock=ManualClock(NOW)
    )
    assert again.deduplicated and again.report_id == first.report_id
    assert llm.calls == 1


def test_an_exhausted_daily_budget_defers_visibly_and_never_calls_the_model(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    tight = CFG.model_copy(
        update={"documents": CFG.documents.model_copy(update={"daily_token_budget": 1})}
    )
    first_llm = ScriptedLlm(_reply())
    analyze_version(
        session_factory, first_llm, config=tight, version_id=version, clock=ManualClock(NOW)
    )
    second = _version(session_factory, BlobStore(tmp_path / "b"), data=TEXT + b" More.")
    llm = ScriptedLlm(_reply())
    out = analyze_version(
        session_factory, llm, config=tight, version_id=second, clock=ManualClock(NOW)
    )
    assert (out.status, out.reason) == ("DEFERRED", "BUDGET_EXHAUSTED")
    assert llm.calls == 0


def test_a_model_failure_is_recorded_not_raised(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    out = analyze_version(
        session_factory,
        ScriptedLlm(LlmError("endpoint down")),
        config=CFG,
        version_id=version,
        clock=ManualClock(NOW),
    )
    assert (out.status, out.reason) == ("FAILED", "endpoint down")


def test_a_failed_call_still_charges_its_token_bound(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Review finding: refused and failed calls spend tokens; the spend must still be recorded."""
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    out = analyze_version(
        session_factory,
        ScriptedLlm(LlmError("endpoint down")),
        config=CFG,
        version_id=version,
        clock=ManualClock(NOW),
    )
    db.rollback()
    tokens = db.execute(
        text("select prompt_tokens + completion_tokens from research_reports where id = :i"),
        {"i": str(out.report_id)},
    ).scalar_one()
    assert tokens >= CFG.agents.llm.max_tokens


def test_the_budget_reserves_the_worst_case_before_calling_the_model(
    db: Session, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    # Enough for a short document's real answer, but not for its worst-case completion.
    tight = CFG.model_copy(
        update={"documents": CFG.documents.model_copy(update={"daily_token_budget": 500})}
    )
    llm = ScriptedLlm(_reply())
    out = analyze_version(session_factory, llm, config=tight, version_id=version, clock=ManualClock(NOW))
    assert (out.status, llm.calls) == ("DEFERRED", 0)
