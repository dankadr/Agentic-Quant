"""Release identity: which code, configuration and schema a process is running.

The revision and image digest are injected at build/deploy time (``TRADING_RELEASE_REVISION``,
``TRADING_RELEASE_IMAGE``). The configuration fingerprint and schema head are derived from the
files this process actually loaded, so they cannot drift from what runs.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReleaseInfo:
    revision: str
    image: str
    config_sha256: str
    schema_head: str | None


def config_fingerprint(config_dir: Path) -> str:
    """SHA-256 over every YAML file in ``config_dir``, in name order. Stable across machines."""
    digest = hashlib.sha256()
    for path in sorted(config_dir.glob("*.yaml")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def release_info(
    *, revision: str, image: str, config_dir: Path, schema_head: str | None
) -> ReleaseInfo:
    return ReleaseInfo(
        revision=revision or UNKNOWN,
        image=image or UNKNOWN,
        config_sha256=config_fingerprint(config_dir),
        schema_head=schema_head,
    )
