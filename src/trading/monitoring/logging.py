"""Structured JSON logging with secret redaction. Secret values never reach a log line."""

import logging
import re
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog
from structlog.typing import FilteringBoundLogger

_SENSITIVE_KEY = re.compile(r"(?i)(password|secret|token|api[_-]?key|authorization)$")
_SECRET_PATTERNS = (
    re.compile(r"Bearer\s+\S+"),
    re.compile(r"trk_[A-Za-z0-9_-]+"),
)
_MASK = "***"


def scrub_text(value: str) -> str:
    """Mask secret-looking substrings (bearer tokens, API keys) in free text."""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_MASK, value)
    return value


def _scrub(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Mapping):
        return _scrub_mapping(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_scrub(v) for v in value]
    # Unknown objects (exceptions, models, ...) are stringified so their text is scrubbed too.
    return scrub_text(str(value))


def _scrub_mapping(data: Mapping[Any, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for raw_key, value in data.items():
        key = str(raw_key)
        if _SENSITIVE_KEY.search(key) and not key.endswith("_file"):
            out[key] = _MASK
        else:
            out[key] = _scrub(value)
    return out


def redact(
    logger: object, method: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor masking sensitive keys and secret-looking strings."""
    return _scrub_mapping(event)


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


def bind_request_id(request_id: str) -> None:
    structlog.contextvars.bind_contextvars(request_id=request_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def get_logger(name: str) -> FilteringBoundLogger:
    logger: FilteringBoundLogger = structlog.get_logger(name)
    return logger
