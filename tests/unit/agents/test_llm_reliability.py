"""The model client's bounds: one schema-repair attempt, and a total deadline per request."""

import json

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

import trading.agents.llm as llm_module
from trading.agents.llm import LlmClient, LlmError, LlmInvalidOutput
from trading.core.config import LlmConfig


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regime: str


def _reply(content: str, status: int = 200) -> httpx.Response:
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }
    return httpx.Response(status, content=json.dumps(body).encode())


def _client(handler, *, timeout: int = 120, sleep=lambda _s: None) -> LlmClient:  # type: ignore[no-untyped-def]
    return LlmClient(
        LlmConfig(base_url="https://llm.example/v1", model="qwen-test", timeout_seconds=timeout),
        api_key="k",
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )


def test_one_repair_attempt_recovers_an_invalid_reply() -> None:
    replies = iter([_reply('{"regime": 5, "extra": true}'), _reply('{"regime": "TRENDING"}')])
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return next(replies)

    result = _client(handler).complete_json("sys", "user", Answer)

    assert result.value.regime == "TRENDING"
    assert len(seen) == 2
    # Tokens from both model calls are counted: the repair is real spend.
    assert (result.prompt_tokens, result.completion_tokens) == (22, 14)


def test_the_repair_request_shows_the_model_its_rejection() -> None:
    replies = iter([_reply("not json at all"), _reply('{"regime": "RANGING"}')])
    seen: list[dict] = []  # type: ignore[type-arg]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return next(replies)

    _client(handler).complete_json("sys", "user", Answer)

    repair_messages = seen[1]["messages"]
    assert repair_messages[-2]["role"] == "assistant"
    assert "rejected" in repair_messages[-1]["content"]


def test_a_second_invalid_reply_is_final_after_exactly_two_calls() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return _reply("still not json")

    with pytest.raises(LlmInvalidOutput):
        _client(handler).complete_json("sys", "user", Answer)
    assert calls["n"] == 2  # never a third attempt at schema repair


def test_retries_stop_when_the_request_deadline_is_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that keeps returning 503 must not hold the caller for more than the deadline."""
    now = {"t": 0.0}
    monkeypatch.setattr(llm_module.time, "monotonic", lambda: now["t"])
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    def sleep(seconds: float) -> None:
        now["t"] += seconds

    # timeout 1 s -> a 2 s total budget per request; the first backoff alone (2 s) uses it up.
    with pytest.raises(LlmError, match="deadline"):
        _client(handler, timeout=1, sleep=sleep).complete_json("sys", "user", Answer)
    assert calls["n"] == 1
    assert now["t"] <= 2.0
