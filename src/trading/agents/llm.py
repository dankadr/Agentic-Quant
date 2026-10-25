"""Minimal OpenAI-compatible chat client for the research agents.

The model is untrusted text in, untrusted text out: its reply is parsed as JSON and validated
against a strict Pydantic schema (``extra="forbid"``), so an injected instruction can at worst
produce a REJECTED output. The client has no tools, no function calling and no access to anything
but one HTTPS endpoint.
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
    """The reply was not valid JSON for the requested schema."""


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
        if char == "{":  # every agent schema is a JSON object; stray lists in prose are skipped
            try:
                value, _ = decoder.raw_decode(cleaned[start:])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise LlmInvalidOutput("no JSON object in the model reply")


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

    def complete_json[T: BaseModel](self, system: str, user: str, schema: type[T]) -> LlmResult[T]:
        payload = {
            "model": self._config.model,
            "temperature": 0,
            "max_tokens": self._config.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        body = self._post(payload)
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("model reply has no choices") from exc
        if choice.get("finish_reason") == "length":
            raise LlmInvalidOutput(
                "model reply was truncated by max_tokens; raise agents.llm.max_tokens"
            )
        data = _extract_json(str(content))
        try:
            value = schema.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"]) or "<root>"
            raise LlmInvalidOutput(
                f"reply does not match {schema.__name__}: {exc.error_count()} errors, "
                f"first at {where} ({first['type']})"
            ) from exc
        usage = body.get("usage") or {}
        return LlmResult(
            value=value,
            model=self._config.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last = ""
        for attempt in range(MAX_ATTEMPTS):
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
                self._sleep(2.0 * 2**attempt)
        raise LlmError(f"model endpoint unavailable after {MAX_ATTEMPTS} attempts ({last})")
