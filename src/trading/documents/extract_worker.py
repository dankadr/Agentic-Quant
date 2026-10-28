"""Child process that parses one document. Run as ``python -I -m trading.documents.extract_worker``.

It reads the raw bytes on stdin and writes one JSON object to stdout. It is started with an empty
environment and holds no credentials. Every failure is reported as data, never as an exception that
could leak parser internals into the caller.
"""

import io
import json
import sys
from html.parser import HTMLParser

_SKIPPED_TAGS = frozenset({"script", "style", "noscript", "template", "iframe", "object", "embed"})


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:  # type: ignore[type-arg]
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def _html_text(data: bytes) -> str:
    parser = _VisibleText()
    parser.feed(data.decode("utf-8", errors="replace"))
    return " ".join(" ".join(parser.parts).split())


def _pdf_pages(data: bytes, max_pages: int) -> dict[str, object]:
    from pypdf import PdfReader  # noqa: PLC0415 - imported only in the child process

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        return {"error": "ENCRYPTED"}
    if len(reader.pages) > max_pages:
        return {"error": "TOO_MANY_PAGES"}
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    if not any(pages):
        return {"status": "OCR_REQUIRED", "pages": pages}  # scanned image: OCR is out of scope
    return {"status": "EXTRACTED", "pages": pages}


def main(argv: list[str]) -> int:
    kind, max_pages = argv[1], int(argv[2])
    data = sys.stdin.buffer.read()
    try:
        if kind == "application/pdf":
            result = _pdf_pages(data, max_pages)
        elif kind == "text/html":
            result = {"status": "EXTRACTED", "pages": [_html_text(data)]}
        else:
            result = {"status": "EXTRACTED", "pages": [data.decode("utf-8")]}
    except Exception as exc:
        result = {"error": f"PARSE_FAILED:{type(exc).__name__}"}
    sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
