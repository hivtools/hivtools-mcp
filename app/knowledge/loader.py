"""Reads the hand-authored knowledge files that sit alongside this module.

Five YAML files plus a markdown document, all version-controlled and reviewable
by a domain expert. They hold what the model outputs do *not* carry:

- ``instructions.md``          the dataset-wide semantic document, served as the
  MCP server's ``instructions`` field so it reaches the model before any tool call.
- ``concepts_analysis.yaml``   plain-language analysis phrases ("treatment gap")
  mapped to the indicators that encode them.
- ``concepts_outofscope.yaml`` plain-language phrases for questions this dataset
  cannot answer at all ("viral suppression", "TB", "funding") - same schema as
  ``concepts_analysis.yaml`` (``answerable: false``, no indicators), merged into
  the same ``concepts()`` dict and the same ``field="concept"`` search results,
  so a decline is not a dead end - each carries a ``notes`` pointer to where the
  answer might actually be found.
- ``concepts_methodology.yaml`` questions about how the models work, not what
  they output ("how does Naomi estimate prevalence?"). No indicators to query -
  loaded separately as ``methodology()`` and served under ``field="methodology"``.
- ``dimensions.yaml``  which age-group sets may safely be summed, and the
  plain-language names people use for age groups and risk groups. The age groups
  mix a partition with overlapping aggregates, and nothing in the source metadata
  says which is which; nor does anything there record that "children" means 0-14,
  or that "FSW" means the ``sexpaid12m`` risk group.
- ``indicators.yaml``  per-indicator units and search aliases. Units are not in
  the source metadata (its format/scale columns are empty), and getting them
  wrong misreports a proportion by 100x.

Known data artefacts are deliberately absent: describing why a modelled value
looks wrong is an epidemiologist's call, not something to infer from the numbers.

Everything here is **universal to the models' output** (Naomi, Spectrum and
SHIPP), not specific to one country or model run. Labels, hierarchy and sort order come from the Naomi zip's
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
    """Plain-language concept -> indicator mappings, plus out-of-scope guardrails.

    Merges ``concepts_analysis.yaml`` (question -> indicators) and
    ``concepts_outofscope.yaml`` (question -> decline + pointer, ``answerable:
    false``) into one dict, since both share the same entry schema and both are
    served under ``field="concept"`` - the caller should not have to know which
    file a match came from. Excludes ``reporting_instruction`` - that is a rule
    for the model, not a searchable entry; get it from ``reporting_instruction()``.
    """
    analysis = {key: value for key, value in _load_yaml("concepts_analysis").items() if key != "reporting_instruction"}
    out_of_scope = _load_yaml("concepts_outofscope")
    overlap = set(analysis) & set(out_of_scope)
    if overlap:
        message = f"concept id(s) in both concepts_analysis.yaml and concepts_outofscope.yaml: {sorted(overlap)}"
        raise ValueError(message)
    return {**analysis, **out_of_scope}


def reporting_instruction() -> str:
    """The plotting/summary-format rule that must accompany every answerable concept match.

    Lives as data in concepts_analysis.yaml, not just a comment, because a
    comment never reaches the model - this has to be read out of the file and
    actually surfaced (in instructions.md and on every field="concept" match).
    """
    return _load_yaml("concepts_analysis")["reporting_instruction"]


def _methodology_yaml() -> dict[str, Any]:
    return _load_yaml("concepts_methodology")


def methodology() -> dict[str, Any]:
    """Model-methodology Q&A: how Naomi, Spectrum and SHIPP produce their estimates.

    Excludes ``answering_instruction`` - that is a rule for the model, not a
    searchable entry; get it from ``methodology_instruction()``.
    """
    return {key: value for key, value in _methodology_yaml().items() if key != "answering_instruction"}


def methodology_instruction() -> str:
    """The DO-NOT-INFER rule that must accompany every methodology answer.

    Lives as data in concepts_methodology.yaml, not just a comment, because a
    comment never reaches the model - this has to be read out of the file and
    actually surfaced (in instructions.md and the search tool description).
    """
    return _methodology_yaml()["answering_instruction"]


def dimensions() -> dict[str, Any]:
    """Per-dimension semantics: age-group partitions, and age- and risk-group aliases."""
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


def risk_group_aliases() -> dict[str, list[str]]:
    """Plain-language names for behavioural risk groups, keyed by risk group code."""
    return dimensions()["risk_group"].get("aliases", {})
