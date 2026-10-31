"""Decide what a document really is from its bytes, never from its name or declared type alone."""

ALLOWED_TYPES = frozenset({"application/pdf", "text/html", "text/plain"})


def sniff(data: bytes) -> str | None:
    """The true type of ``data``, or None if it is none of the accepted types."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    head = data[:4096]
    if b"\x00" in head:
        return None  # binary content that is not a PDF
    lowered = head.lower()
    if b"<html" in lowered or b"<!doctype html" in lowered:
        return "text/html"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return "text/plain"
