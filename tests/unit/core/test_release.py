from pathlib import Path

from trading.core.release import UNKNOWN, config_fingerprint, release_info


def _configs(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "risk.yaml").write_text("max: 1\n")
    (root / "system.yaml").write_text("mode: PAPER_ONLY\n")
    return root


def test_fingerprint_is_stable_for_identical_contents(tmp_path: Path) -> None:
    a = _configs(tmp_path / "a")
    b = _configs(tmp_path / "b")
    assert config_fingerprint(a) == config_fingerprint(b)


def test_fingerprint_changes_when_any_config_changes(tmp_path: Path) -> None:
    root = _configs(tmp_path / "c")
    before = config_fingerprint(root)
    (root / "risk.yaml").write_text("max: 2\n")
    assert config_fingerprint(root) != before


def test_fingerprint_binds_file_names_not_only_contents(tmp_path: Path) -> None:
    root = _configs(tmp_path / "d")
    before = config_fingerprint(root)
    (root / "risk.yaml").rename(root / "limits.yaml")
    assert config_fingerprint(root) != before


def test_missing_revision_and_image_report_unknown(tmp_path: Path) -> None:
    info = release_info(
        revision="", image="", config_dir=_configs(tmp_path / "e"), schema_head="0003"
    )
    assert (info.revision, info.image, info.schema_head) == (UNKNOWN, UNKNOWN, "0003")
    assert len(info.config_sha256) == 64
