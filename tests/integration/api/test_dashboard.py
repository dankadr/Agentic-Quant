"""The dashboard: key exchanged for a server session, CSRF enforced, and stored text escaped."""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.api.conftest import bearer
from trading.core.config import Settings
from trading.documents.blobs import BlobStore

BODY = (
    "Quarterly results. Management raised full-year guidance. "
    "<script>alert('x')</script> Stored text is shown escaped."
)


def _plain(client: TestClient) -> TestClient:
    client.app.state.settings = Settings(ui_cookie_secure=False)  # tests run over plain http
    return client


def _login(client: TestClient, key: str) -> str:
    page = client.get("/ui/login")
    csrf = page.cookies.get("ui_login") or ""
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    assert token == csrf
    response = client.post(
        "/ui/login", data={"api_key": key, "csrf": token}, follow_redirects=False
    )
    assert response.status_code == 303
    return response.cookies.get("ui_session") or ""


def test_an_api_key_opens_a_server_session_and_the_key_is_not_kept(
    client: TestClient, viewer_key: str
) -> None:
    client = _plain(client)
    cookie = _login(client, viewer_key)
    assert cookie and viewer_key not in cookie
    page = client.get("/ui")
    assert page.status_code == 200 and "Overview" in page.text


def test_a_wrong_key_is_refused(client: TestClient) -> None:
    client = _plain(client)
    page = client.get("/ui/login")
    token = page.text.split('name="csrf" value="')[1].split('"')[0]
    response = client.post("/ui/login", data={"api_key": "trk_wrong", "csrf": token})
    assert response.status_code == 401


def test_pages_need_a_session(client: TestClient) -> None:
    client = _plain(client)
    response = client.get("/ui/orders", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/ui/login"


def test_logout_needs_the_csrf_token(client: TestClient, viewer_key: str) -> None:
    client = _plain(client)
    _login(client, viewer_key)
    refused = client.post("/ui/logout", data={"csrf": "forged"}, follow_redirects=False)
    assert refused.status_code == 403
    assert client.get("/ui", follow_redirects=False).status_code == 200  # still signed in


def test_an_operator_acknowledges_an_alert_with_csrf_and_a_viewer_cannot(
    client: TestClient,
    operator_key: str,
    viewer_key: str,
    session_factory,  # type: ignore[no-untyped-def]
) -> None:
    from datetime import UTC, datetime

    from trading.alerts.store import raise_incident

    with session_factory.begin() as s:
        raise_incident(
            s, key="k", severity="WARNING", title="Research failing", detail="x",
            now=datetime(2026, 10, 28, 15, tzinfo=UTC),
        )  # fmt: skip
    client = _plain(client)
    _login(client, viewer_key)
    page = client.get("/ui/alerts").text
    assert "/ack" not in page  # viewers see the incident but get no acknowledge form


def test_stored_research_text_is_escaped_on_the_page(
    client: TestClient, operator_key: str, viewer_key: str, tmp_path: Path
) -> None:
    import base64

    client.app.state.blobs = BlobStore(tmp_path / "b")  # type: ignore[attr-defined]
    client.post(
        "/v1/research/documents",
        headers=bearer(operator_key),
        json={
            "title": "<img src=x onerror=alert(1)> Q3",
            "category": "FINANCIAL_STATEMENT",
            "source": "ir",
            "content_type": "text/plain",
            "content_base64": base64.b64encode(BODY.encode()).decode(),
        },
    )
    client = _plain(client)
    _login(client, viewer_key)
    page = client.get("/ui/research", params={"q": "guidance"}).text
    assert "<img src=x" not in page and "<script>alert" not in page
    assert "&lt;img src=x" in page or "&lt;script&gt;" in page
