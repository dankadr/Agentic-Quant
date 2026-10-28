"""Run the parser in an isolated child process with a hard time limit."""

import json
import resource
import subprocess
import sys
from dataclasses import dataclass

MAX_TEXT_CHARS = 2_000_000  # the child refuses to emit more; the parent refuses to accept more
_MEMORY_BYTES = 512 * 1024 * 1024


def _limit_resources() -> None:
    """Cap the child's address space where the platform enforces it (time limit always applies)."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_BYTES, _MEMORY_BYTES))
    except (ValueError, OSError):
        pass


@dataclass(frozen=True)
class Extraction:
    status: str  # EXTRACTED | OCR_REQUIRED | FAILED
    pages: tuple[str, ...]
    error: str | None


def extract(data: bytes, kind: str, *, max_pages: int, timeout_seconds: int) -> Extraction:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no inherited environment
            [sys.executable, "-I", "-m", "trading.documents.extract_worker", kind, str(max_pages)],
            input=data,
            capture_output=True,
            timeout=timeout_seconds,
            env={},
            check=False,
            preexec_fn=_limit_resources,
        )
    except subprocess.TimeoutExpired:
        return Extraction("FAILED", (), "TIMEOUT")
    try:
        body = json.loads(proc.stdout or b"{}")
    except ValueError:
        return Extraction("FAILED", (), f"WORKER_EXIT_{proc.returncode}")
    if "error" in body:
        return Extraction("FAILED", (), str(body["error"]))
    status = str(body.get("status", "FAILED"))
    pages = tuple(str(p) for p in body.get("pages", []))
    if sum(len(p) for p in pages) > MAX_TEXT_CHARS:
        return Extraction("FAILED", (), "TEXT_TOO_LARGE")
    return Extraction(status, pages, None)
