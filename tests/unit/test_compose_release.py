from pathlib import Path

import yaml


def test_migration_and_application_build_identical_revision_images() -> None:
    compose = yaml.safe_load(Path("docker-compose.yml").read_text())
    app = compose["x-app"]
    migrate = compose["services"]["migrate"]
    assert migrate["image"] == app["image"]
    assert migrate["build"] == app["build"]
