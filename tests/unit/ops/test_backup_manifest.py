import json
from pathlib import Path

from trading.ops.backup import MANIFEST_NAME, build_manifest, verify_manifest, write_manifest


def _backup(root: Path) -> None:
    (root / "dump.custom").write_bytes(b"database bytes")
    (root / "docstore").mkdir()
    (root / "docstore" / "blob.bin").write_bytes(b"document bytes")


def test_an_intact_backup_verifies_clean(tmp_path: Path) -> None:
    _backup(tmp_path)
    write_manifest(tmp_path)
    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text())
    assert verify_manifest(tmp_path, manifest) == []


def test_a_truncated_file_is_caught(tmp_path: Path) -> None:
    _backup(tmp_path)
    manifest = build_manifest(tmp_path)
    (tmp_path / "dump.custom").write_bytes(b"database")
    assert verify_manifest(tmp_path, manifest) == ["size changed: dump.custom"]


def test_a_substituted_file_of_the_same_size_is_caught(tmp_path: Path) -> None:
    _backup(tmp_path)
    manifest = build_manifest(tmp_path)
    (tmp_path / "dump.custom").write_bytes(b"DATABASE bytes")
    assert verify_manifest(tmp_path, manifest) == ["checksum changed: dump.custom"]


def test_missing_and_unexpected_files_are_reported(tmp_path: Path) -> None:
    _backup(tmp_path)
    manifest = build_manifest(tmp_path)
    (tmp_path / "docstore" / "blob.bin").unlink()
    (tmp_path / "extra.txt").write_text("not in the backup")
    problems = verify_manifest(tmp_path, manifest)
    assert "missing: docstore/blob.bin" in problems
    assert "unexpected file: extra.txt" in problems
