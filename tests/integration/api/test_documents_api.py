import base64
from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.api.conftest import bearer
from trading.documents.blobs import BlobStore

REPORT = b"Quarterly results. Management raised full-year guidance."


def _body(data: bytes = REPORT, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "title": "Q3 earnings release",
        "category": "FINANCIAL_STATEMENT",
        "company": "aapl",
        "source": "issuer-ir",
        "content_type": "text/plain",
        "content_base64": base64.b64encode(data).decode(),
        "published_at": "2026-10-15T12:00:00Z",
        "reporting_period": "2026Q3",
    }
    body.update(overrides)
    return body


def _client_with_blobs(client: TestClient, tmp_path: Path) -> TestClient:
    client.app.state.blobs = BlobStore(tmp_path / "blobs")  # type: ignore[attr-defined]
    return client


def test_operator_uploads_a_document_and_a_repeat_is_deduplicated(
    client: TestClient, operator_key: str, tmp_path: Path
) -> None:
    client = _client_with_blobs(client, tmp_path)
    first = client.post("/v1/research/documents", headers=bearer(operator_key), json=_body())
    assert first.status_code == 201
    assert first.json()["deduplicated"] is False
    again = client.post("/v1/research/documents", headers=bearer(operator_key), json=_body())
    assert again.json()["deduplicated"] is True


def test_viewers_cannot_upload(client: TestClient, viewer_key: str, tmp_path: Path) -> None:
    client = _client_with_blobs(client, tmp_path)
    response = client.post("/v1/research/documents", headers=bearer(viewer_key), json=_body())
    assert response.status_code == 403


def test_a_mislabelled_upload_is_refused_with_a_reason(
    client: TestClient, operator_key: str, tmp_path: Path
) -> None:
    client = _client_with_blobs(client, tmp_path)
    response = client.post(
        "/v1/research/documents",
        headers=bearer(operator_key),
        json=_body(content_type="text/html"),
    )
    assert response.status_code == 422
    assert response.json()["code"] == "TYPE_MISMATCH"


def test_url_import_refuses_a_host_that_is_not_approved(
    client: TestClient, operator_key: str, tmp_path: Path
) -> None:
    client = _client_with_blobs(client, tmp_path)
    response = client.post(
        "/v1/research/import-url",
        headers=bearer(operator_key),
        json={
            "url": "https://www.sec.gov/Archives/example.htm",
            "title": "10-Q",
            "category": "FINANCIAL_STATEMENT",
            "source": "sec",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "HOST_NOT_ALLOWED"


def test_search_and_citation_are_available_to_viewers(
    client: TestClient,
    operator_key: str,
    viewer_key: str,
    tmp_path: Path,
) -> None:
    client = _client_with_blobs(client, tmp_path)
    uploaded = client.post(
        "/v1/research/documents", headers=bearer(operator_key), json=_body()
    ).json()
    hits = client.get(
        "/v1/research/documents",
        headers=bearer(viewer_key),
        params={"q": "guidance", "company": "AAPL"},
    ).json()["items"]
    assert [h["version_id"] for h in hits] == [uploaded["version_id"]]
    quote = client.get(
        "/v1/research/citations",
        headers=bearer(viewer_key),
        params={"version_id": uploaded["version_id"], "page": 1, "start": 0, "end": 17},
    )
    assert quote.status_code == 200
    assert quote.json()["text"] == "Quarterly results"
