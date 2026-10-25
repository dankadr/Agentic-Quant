"""Parameter grids for research (configs/research.yaml)."""

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _Grid(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    space: dict[str, list[Any]] = Field(min_length=1)


class _File(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    research: dict[str, _Grid]


def load_research_spaces(config_dir: Path) -> Mapping[str, Mapping[str, Sequence[Any]]]:
    path = config_dir / "research.yaml"
    if not path.exists():
        return {}
    parsed = _File.model_validate(yaml.safe_load(path.read_text()))
    return {sid: grid.space for sid, grid in parsed.research.items()}
