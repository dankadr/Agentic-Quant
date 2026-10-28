"""Content-addressed, write-once blob store for original documents.

A document is stored under its SHA-256. Writing the same bytes twice is a no-op, and existing
content is never rewritten, so a citation to a checksum always resolves to the same bytes.
"""

import hashlib
import os
import tempfile
from pathlib import Path

from trading.core.errors import TradingError


class BlobIntegrityError(TradingError):
    """A stored blob no longer matches its checksum."""


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256[2:4] / sha256

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if path.exists():
            return sha  # identical content already stored; never overwrite it
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)  # atomic: readers see the whole file or none of it
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return sha

    def get(self, sha256: str) -> bytes:
        data = self._path(sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise BlobIntegrityError(f"blob {sha256} does not match its checksum")
        return data
