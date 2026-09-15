"""Reads the hand-authored knowledge files that sit alongside this module.

Three YAML files plus a markdown document, all version-controlled and reviewable
by a domain expert. They hold what the Naomi model output does *not* carry:

- ``instructions.md``  the dataset-wide semantic document, served as the MCP
  server's ``instructions`` field so it reaches the model before any tool call.
- ``concepts.yaml``    plain-language phrases ("treatment gap") mapped to the
  indicators that encode them.
- ``dimensions.yaml``  which age-group sets may safely be summed, and the
  plain-language names people use for age groups. The age groups mix a partition
  with overlapping aggregates, and nothing in the source metadata says which is
  which; nor does anything there record that "children" means 0-14.
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

from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml

_DIR = Path(__file__).parent


@cache
def _load_yaml(name: str) -> dict[str, Any]:
    with (_DIR / f"{name}.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def concepts() -> dict[str, Any]:
    """Plain-language concept -> indicator mappings."""
    return _load_yaml("concepts")


def dimensions() -> dict[str, Any]:
    """Per-dimension semantics. Currently only age-group partitions."""
    return _load_yaml("dimensions")


def indicators() -> dict[str, Any]:
    """Per-indicator units, basis and search aliases."""
    return _load_yaml("indicators")


@lru_cache(maxsize=1)
def instructions() -> str:
    """The semantic document, for the MCP server's ``instructions`` field."""
    return (_DIR / "instructions.md").read_text(encoding="utf-8")


def age_aliases() -> dict[str, list[str]]:
    """Plain-language names for age groups, keyed by age group code."""
    return dimensions()["age_group"].get("aliases", {})


def age_alias_index() -> dict[str, list[str]]:
    """Inverted aliases: a normalised phrase -> the age group codes it may mean.

    The value is a list because the ambiguity is real and worth preserving.
    "adults" maps to both Y015_049 and Y015_999; a resolver should surface both
    and let the caller choose rather than picking one silently.
    """
    index: dict[str, list[str]] = {}
    for age_group, aliases in age_aliases().items():
        for alias in aliases:
            index.setdefault(alias.strip().lower(), []).append(age_group)
    return index


def age_partitions() -> dict[str, dict[str, Any]]:
    """Age-group sets that may safely be summed, keyed by name.

    Each spec carries ``values`` and the ``total`` those values tile. The total is
    the all-ages group unless the partition says otherwise: a partition may tile a
    sub-population, as the single-year bands under 15 tile Y000_014.
    """
    age_group = dimensions()["age_group"]
    return {name: {"total": age_group["total_value"], **spec} for name, spec in age_group["partitions"].items()}
