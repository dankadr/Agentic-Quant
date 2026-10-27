"""Minimal OpenAI-compatible chat client for the research agents.

The model is untrusted text in, untrusted text out: its reply is parsed as JSON and validated
against a strict Pydantic schema (``extra="forbid"``), so an injected instruction can at worst
produce a REJECTED output. The client has no tools, no function calling and no access to anything
but one HTTPS endpoint.

Reliability bounds: transport failures are retried, but only within one total deadline per
request. A reply that is not valid for its schema gets exactly one repair attempt, and then it is
rejected. Token usage from every model call is summed into the result.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from trading.core.config import LlmConfig
from trading.core.errors import TradingError
from trading.monitoring.logging import get_logger

_log = get_logger(__name__)
MAX_ATTEMPTS = 3
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LlmError(TradingError):
    """The model endpoint failed or answered nonsense at the transport level."""


class LlmInvalidOutput(LlmError):
    """The reply was not valid JSON for the requested schema (after the one repair attempt)."""


@dataclass(frozen=True)
class LlmResult[T: BaseModel]:
    value: T
    model: str
    prompt_tokens: int
    completion_tokens: int


def _extract_json(text: str) -> Any:
    cleaned = _THINK.sub("", text).strip()
    fence = _FENCE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()
    decoder = json.JSONDecoder()
    for start, char in enumerate(cleaned):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(cleaned[start:])
            except ValueError:
                continue
            return value
    raise LlmInvalidOutput("no JSON object in the model reply")


def _content(body: dict[str, Any]) -> str:
    try:
        return str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("model reply has no choices") from exc


def _usage(body: dict[str, Any]) -> tuple[int, int]:
    usage = body.get("usage") or {}
    return int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


class LlmClient:
    def __init__(
        self,
        config: LlmConfig,
        *,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._sleep = sleep
        # One request may spend at most this long across all of its retries.
        self._deadline_seconds = float(config.timeout_seconds) * 2
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/"),
            headers=headers,
            timeout=float(config.timeout_seconds),
            transport=transport,
        )

    @classmethod
    def from_file(cls, config: LlmConfig, key_file: Path, **kwargs: Any) -> "LlmClient":
        key = key_file.read_text().strip() if key_file.exists() else ""
        return cls(config, api_key=key, **kwargs)

    def __repr__(self) -> str:
        return f"LlmClient(model={self._config.model!r})"  # never includes the key

    def close(self) -> None:
        self._client.close()

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self._config.model,
            "temperature": 0,
            "max_tokens": self._config.max_tokens,
            "messages": messages,
        }

    def _parse[T: BaseModel](self, body: dict[str, Any], schema: type[T]) -> T:
        data = _extract_json(_content(body))
        try:
            return schema.model_validate(data)
        except ValidationError as exc:
            raise LlmInvalidOutput(
                f"reply does not match {schema.__name__}: {exc.error_count()} errors"
            ) from exc

    def complete_json[T: BaseModel](self, system: str, user: str, schema: type[T]) -> LlmResult[T]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        body = self._post(self._payload(messages))
        prompt_tokens, completion_tokens = _usage(body)
        try:
            value = self._parse(body, schema)
        except LlmInvalidOutput as exc:
            # One repair attempt: show the model its own reply and the exact rejection, then ask
            # once more. A second invalid reply is final.
            _log.info("llm_schema_repair", schema=schema.__name__)
            messages += [
                {"role": "assistant", "content": _content(body)},
                {
                    "role": "user",
                    "content": f"That reply was rejected: {exc}. Reply again with only"
                    " the corrected JSON object that matches the schema.",
                },
            ]
            repaired = self._post(self._payload(messages))
            prompt_tokens += _usage(repaired)[0]
            completion_tokens += _usage(repaired)[1]
            value = self._parse(repaired, schema)
        return LlmResult(
            value=value,
            model=self._config.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self._deadline_seconds
        last = ""
        for attempt in range(MAX_ATTEMPTS):
            if attempt and time.monotonic() >= deadline:
                last = "request deadline reached"
                break
            try:
                response = self._client.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                last = f"network error: {type(exc).__name__}"
            else:
                if response.status_code in (401, 403):
                    raise LlmError(
                        f"model endpoint rejected the credentials (HTTP {response.status_code})"
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    last = f"HTTP {response.status_code}"
                elif response.status_code != 200:
                    raise LlmError(f"model endpoint failed (HTTP {response.status_code})")
                else:
                    try:
                        decoded: dict[str, Any] = response.json()
                    except ValueError as exc:
                        raise LlmError("model endpoint returned non-JSON") from exc
                    return decoded
            if attempt < MAX_ATTEMPTS - 1:
                remaining = max(0.0, deadline - time.monotonic())
                self._sleep(min(2.0 * 2**attempt, remaining))
        raise LlmError(f"model endpoint unavailable ({last})")
