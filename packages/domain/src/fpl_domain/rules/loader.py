"""Load and cache rulesets from ``config/rules``."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from fpl_domain.rules.model import Ruleset

_ENV_CONFIG_DIR = "FPL_CONFIG_DIR"


def config_root() -> Path:
    """Locate the repository ``config/`` directory.

    Resolution order: ``$FPL_CONFIG_DIR``; otherwise walk up from this file and from the CWD.
    """
    env = os.environ.get(_ENV_CONFIG_DIR)
    if env:
        p = Path(env)
        if not p.is_dir():
            raise FileNotFoundError(f"{_ENV_CONFIG_DIR}={env} is not a directory")
        return p
    for start in (Path(__file__).resolve(), Path.cwd().resolve()):
        for parent in (start, *start.parents):
            candidate = parent / "config" / "rules"
            if candidate.is_dir():
                return parent / "config"
    raise FileNotFoundError("could not locate config/ directory; set FPL_CONFIG_DIR")


def parse_ruleset(data: dict[str, Any]) -> Ruleset:
    return Ruleset.model_validate(data)


def load_ruleset_file(path: Path) -> Ruleset:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ruleset must be a mapping")
    return parse_ruleset(data)


@lru_cache(maxsize=32)
def load_ruleset(season: str) -> Ruleset:
    """Load the ruleset for a season code such as ``"2026-27"``."""
    path = config_root() / "rules" / f"{season}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no ruleset for season {season!r} at {path}")
    rs = load_ruleset_file(path)
    if rs.season != season:
        raise ValueError(f"{path} declares season {rs.season!r}")
    return rs


def available_seasons() -> list[str]:
    root = config_root() / "rules"
    return sorted(p.stem for p in root.glob("*.yaml") if p.stem != "schema")
