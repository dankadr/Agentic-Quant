import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from trading.agents.llm import LlmClient, LlmError, LlmInvalidOutput
from trading.core.config import LlmConfig

KEY = "llm-secret-key-123"


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regime: str
    score: float


def client(handler, **kw) -> LlmClient:  # type: ignore[no-untyped-def]
    return LlmClient(
        LlmConfig(base_url="https://llm.example/v1", model="qwen-test"),
        api_key=KEY,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kw,
    )


def reply(content: str, status: int = 200) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return httpx.Response(status, content=json.dumps(body).encode())


def test_request_shape_and_parsed_result() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return reply('{"regime": "RANGING", "score": 0.5}')

    result = client(handler).complete_json("system prompt", "user data", Answer)
    req = seen[0]
    assert str(req.url) == "https://llm.example/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = json.loads(req.content)
    assert body["model"] == "qwen-test"
    assert body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert (result.value.regime, result.value.score) == ("RANGING", 0.5)
    assert (result.prompt_tokens, result.completion_tokens, result.model) == (11, 7, "qwen-test")


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"regime": "RANGING", "score": 1}\n```',
        '<think>hmm, trending? no.</think>\n{"regime": "RANGING", "score": 1}',
        'Sure! Here is the answer: {"regime": "RANGING", "score": 1} hope that helps',
    ],
)
def test_models_wrap_json_in_noise_and_it_is_still_parsed(content: str) -> None:
    assert (
        client(lambda r: reply(content)).complete_json("s", "u", Answer).value.regime == "RANGING"
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        '{"regime": "RANGING"}',
        '{"regime": "RANGING", "score": 1, "tool": "place_order"}',
        "[]",
    ],
)
def test_invalid_or_extra_output_is_rejected(content: str) -> None:
    with pytest.raises(LlmInvalidOutput):
        client(lambda r: reply(content)).complete_json("s", "u", Answer)


def test_server_errors_are_retried_then_surface_without_the_key() -> None:
    calls = {"n": 0}

    def dead(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    with pytest.raises(LlmError) as err:
        client(dead).complete_json("s", "u", Answer)
    assert calls["n"] == 3
    assert KEY not in str(err.value)
    assert KEY not in repr(client(dead))


def test_auth_failure_is_not_retried() -> None:
    calls = {"n": 0}

    def denied(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(LlmError):
        client(denied).complete_json("s", "u", Answer)
    assert calls["n"] == 1


def test_empty_choices_is_an_error() -> None:
    with pytest.raises(LlmError):
        client(lambda r: httpx.Response(200, json={"choices": []})).complete_json("s", "u", Answer)
