"""Reads the hand-authored knowledge files that sit alongside this module.

Three YAML files plus a markdown document, all version-controlled and reviewable
by a domain expert. They hold what the Naomi model output does *not* carry:

- ``instructions.md``  the dataset-wide semantic document, served as the MCP
  server's ``instructions`` field so it reaches the model before any tool call.
- ``concepts.yaml``    plain-language phrases ("treatment gap") mapped to the
  indicators that encode them.
- ``dimensions.yaml``  how each dimension behaves - above all, which value sets
  may be summed. Naively summing all 33 age groups overstates totals by 7.56x.
- ``indicators.yaml``  per-indicator units and search aliases. Units are not in
  the source metadata (its format/scale columns are empty), and getting them
  wrong misreports a proportion by 100x.

Known data artefacts are deliberately absent: describing why a modelled value
looks wrong is an epidemiologist's call, not something to infer from the numbers.

Everything here is **universal to Naomi output**, not specific to one country or
model run. Labels, hierarchy and sort order come from the Naomi zip's
``meta_*.csv`` files at data-prep time; per-country coverage (which quarters,
which area levels, how many districts) is generated from the data and served by
the discovery endpoints, because it differs per country and so cannot live in a
system prompt shared by all of them.

``tests/test_knowledge.py`` checks every claim in these files against the real
dataset, so they cannot drift from the data they describe.
"""

from functools import cache
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).parent


@cache
def _load_yaml(name: str) -> dict[str, Any]:
    with (_DIR / f"{name}.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def indicators() -> dict[str, Any]:
    """Per-indicator units, basis and search aliases."""
    return _load_yaml("indicators")
