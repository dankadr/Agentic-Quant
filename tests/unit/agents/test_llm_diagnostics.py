import httpx
import pytest
from pydantic import BaseModel

from trading.agents.llm import LlmClient, LlmError
from trading.core.config import LlmConfig


class Answer(BaseModel):
    ok: bool


def test_bad_request_is_not_retried_and_exposes_only_safe_category() -> None:
    calls = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            400,
            json={"error": {"code": "model_not_found", "message": "secret-user-prompt api-secret"}},
        )

    client = LlmClient(LlmConfig(), api_key="api-secret", transport=httpx.MockTransport(fail))
    with pytest.raises(LlmError) as error:
        client.complete_json("private", "prompt", Answer)
    assert len(calls) == 1
    assert "model_not_found" in str(error.value)
    assert "api-secret" not in str(error.value)
    assert "secret-user-prompt" not in str(error.value)
    client.close()


def test_untrusted_server_code_is_not_echoed() -> None:
    client = LlmClient(
        LlmConfig(),
        api_key="x",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                400, json={"error": {"code": "my-secret-key", "message": "private"}}
            )
        ),
    )
    with pytest.raises(LlmError) as error:
        client.complete_json("s", "u", Answer)
    assert "bad_request" in str(error.value)
    assert "my-secret-key" not in str(error.value)
    client.close()
