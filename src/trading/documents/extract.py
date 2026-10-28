"""Run the parser in an isolated child process with a hard time limit."""

import json
import subprocess
import sys
from dataclasses import dataclass


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
    return Extraction(status, pages, None)
