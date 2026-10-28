"""The digest is causal: research shows up only from the moment it became available."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from tests.integration.api.conftest import bearer
from tests.integration.reports.test_analysis import CFG, ScriptedLlm, _reply, _version
from trading.core.clock import ManualClock
from trading.documents.blobs import BlobStore
from trading.reports.analyze import analyze_version

WORKED = datetime(2026, 10, 28, 10, 0, tzinfo=UTC)


def test_a_report_is_absent_before_it_is_available_and_present_after(
    client: TestClient,
    viewer_key: str,
    db: Session,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    analyze_version(
        session_factory,
        ScriptedLlm(_reply()),
        config=CFG,
        version_id=version,
        clock=ManualClock(WORKED),
    )
    before = client.get(
        "/v1/research/digest",
        headers=bearer(viewer_key),
        params={"as_of": (WORKED - timedelta(minutes=5)).isoformat()},
    ).json()
    after = client.get(
        "/v1/research/digest",
        headers=bearer(viewer_key),
        params={"as_of": (WORKED + timedelta(minutes=5)).isoformat()},
    ).json()
    assert before["categories"] == {}
    assert list(after["categories"]) == ["FINANCIAL_STATEMENT"]
    (entry,) = after["categories"]["FINANCIAL_STATEMENT"]
    assert entry["fact_count"] == 1
    assert entry["missing"] == ["prior-year comparison"]


def test_report_detail_carries_its_citations(
    client: TestClient,
    viewer_key: str,
    session_factory: sessionmaker[Session],
    tmp_path: Path,
) -> None:
    version = _version(session_factory, BlobStore(tmp_path / "b"))
    out = analyze_version(
        session_factory,
        ScriptedLlm(_reply()),
        config=CFG,
        version_id=version,
        clock=ManualClock(WORKED),
    )
    body = client.get(f"/v1/research/reports/{out.report_id}", headers=bearer(viewer_key)).json()
    assert body["status"] == "COMPLETED"
    (fact,) = body["facts"]
    assert fact["key"] == "revenue" and fact["value"] == "4.2"
    assert fact["citation"]["page"] == 1
