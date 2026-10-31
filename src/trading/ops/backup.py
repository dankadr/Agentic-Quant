"""Backup manifests: a checksum list written beside each backup, verified before any restore.

A manifest lists every file of a backup with its size and SHA-256. A restore first verifies
the manifest, so a truncated, substituted or partly copied backup is refused before it is loaded.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_NAME = "MANIFEST.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(root: Path) -> dict[str, Any]:
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != MANIFEST_NAME)
    return {
        "schema": 1,
        "files": [
            {
                "path": str(p.relative_to(root)),
                "bytes": p.stat().st_size,
                "sha256": _sha256(p),
            }
            for p in files
        ],
    }


def write_manifest(root: Path) -> Path:
    path = root / MANIFEST_NAME
    path.write_text(json.dumps(build_manifest(root), indent=2, sort_keys=True) + "\n")
    return path


def verify_manifest(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Every difference from the manifest, one line each. Empty means the backup is intact."""
    problems: list[str] = []
    expected = {entry["path"]: entry for entry in manifest.get("files", [])}
    present = {
        str(p.relative_to(root)): p
        for p in root.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    }
    for rel, entry in expected.items():
        path = present.get(rel)
        if path is None:
            problems.append(f"missing: {rel}")
            continue
        if path.stat().st_size != entry["bytes"]:
            problems.append(f"size changed: {rel}")
        elif _sha256(path) != entry["sha256"]:
            problems.append(f"checksum changed: {rel}")
    for rel in sorted(set(present) - set(expected)):
        problems.append(f"unexpected file: {rel}")
    return problems
