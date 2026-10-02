"""Export the ruleset JSON-Schema to ``config/rules/schema.yaml``.

Run ``python -m fpl_domain.rules.schema`` after changing the Ruleset model; a unit test fails if
the committed schema and the model drift apart.
"""

from __future__ import annotations

from typing import Any

import yaml

from fpl_domain.rules.loader import config_root
from fpl_domain.rules.model import Ruleset

HEADER = (
    "# GENERATED from fpl_domain.rules.model.Ruleset — do not edit by hand.\n"
    "# Regenerate with: uv run python -m fpl_domain.rules.schema\n"
)


def ruleset_json_schema() -> dict[str, Any]:
    return Ruleset.model_json_schema(mode="validation")


def render_schema_yaml() -> str:
    return HEADER + yaml.safe_dump(ruleset_json_schema(), sort_keys=True, width=100)


def main() -> None:
    path = config_root() / "rules" / "schema.yaml"
    path.write_text(render_schema_yaml(), encoding="utf-8")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
