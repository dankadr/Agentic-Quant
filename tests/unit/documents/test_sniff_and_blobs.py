from pathlib import Path

import pytest

from trading.documents.blobs import BlobIntegrityError, BlobStore
from trading.documents.sniff import sniff


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"%PDF-1.7\n...", "application/pdf"),
        (b"<!DOCTYPE html><html><body>x</body></html>", "text/html"),
        (b"<html><body>x</body></html>", "text/html"),
        (b"Revenue rose 4% in the quarter.", "text/plain"),
        (b"\x89PNG\r\n\x1a\n\x00\x00", None),  # binary, not a PDF
        (b"\xff\xfe\xfa not utf-8", None),
    ],
)
def test_content_is_classified_by_its_bytes(data: bytes, kind: str | None) -> None:
    assert sniff(data) == kind


def test_identical_content_is_stored_once_and_never_overwritten(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    first = store.put(b"same bytes")
    second = store.put(b"same bytes")
    assert first == second
    assert len(list(tmp_path.rglob(first))) == 1


def test_a_corrupted_blob_is_refused_on_read(tmp_path: Path) -> None:
    store = BlobStore(tmp_path)
    sha = store.put(b"original")
    (path,) = tmp_path.rglob(sha)
    path.write_bytes(b"tampered")
    with pytest.raises(BlobIntegrityError):
        store.get(sha)
