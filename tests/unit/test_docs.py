import re
from pathlib import Path

from typer.testing import CliRunner

from trading.cli import app

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def test_every_cli_command_in_the_readme_exists() -> None:
    readme = (ROOT / "README.md").read_text()
    mentioned = set(re.findall(r"uv run trading ((?:admin )?[a-z][a-z-]*)", readme))
    assert {"migrate", "replay", "openapi", "admin create-api-key"} <= mentioned
    for command in sorted(mentioned):
        parts = command.split()
        result = CliRunner().invoke(app, [*parts, "--help"])
        assert result.exit_code == 0, f"README mentions `trading {command}` which does not exist"


def test_relative_markdown_links_resolve() -> None:
    broken = []
    for doc in DOCS:
        if "superpowers" in doc.parts:
            continue  # plans reference files that are created later by design
        for target in re.findall(r"\]\(([^)#]+?)(?:#[^)]*)?\)", doc.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (doc.parent / target).resolve().exists():
                broken.append(f"{doc.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_live_trading_is_not_documented_as_available() -> None:
    text = (ROOT / "README.md").read_text().lower()
    assert "paper-only" in text
    assert "no live trading" in text
