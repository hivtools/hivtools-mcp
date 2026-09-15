"""Checks the hand-authored knowledge layer against the real Naomi dataset.

The files in ``app/knowledge`` make factual claims about the data - which
indicators exist, which age groups form a partition, which indicators can go
negative. Prose that has drifted from the data is worse than no prose, because
the model believes it. Every claim is therefore asserted here against the
demo zip that ships with the repo (about one second to load, so these run
alongside everything else rather than behind a marker).
"""

import zipfile
from pathlib import Path

import duckdb
import pytest

from app.knowledge import loader as knowledge

ZIP = Path(__file__).parent.parent / "data-prep" / "raw-data" / "naomi_output_malawi.zip"

# Reference slice used for every additivity check: one indicator, one quarter,
# national, both sexes. Additivity is a property of the dimensions, so any
# well-populated slice demonstrates it.
REF = "indicator='plhiv' AND calendar_quarter='CY2024Q3' AND area_id='MWI' AND sex='both'"
TOLERANCE = 5e-4


@pytest.fixture(scope="session")
def db(tmp_path_factory: pytest.TempPathFactory) -> duckdb.DuckDBPyConnection:
    """The full demo dataset, read straight out of the committed zip."""
    csv_path = tmp_path_factory.mktemp("naomi") / "indicators.csv"
    with zipfile.ZipFile(ZIP) as archive:
        csv_path.write_bytes(archive.read("indicators.csv"))
    connection = duckdb.connect()
    connection.execute(f"CREATE TABLE ind AS SELECT * FROM read_csv('{csv_path}', header=true)")
    return connection


@pytest.fixture(scope="session")
def dataset_indicators(db: duckdb.DuckDBPyConnection) -> set[str]:
    return {record[0] for record in db.sql("SELECT DISTINCT indicator FROM ind").fetchall()}


def row(db: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> tuple:
    """First row of a query that must return one. DuckDB types ``fetchone()`` as optional."""
    result = db.sql(sql, params=params or []).fetchone()
    assert result is not None, f"query returned no rows: {sql}"
    return result


def scalar(db: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> float:
    return row(db, sql, params)[0]


def all_ages_total(db: duckdb.DuckDBPyConnection) -> float:
    total_value = knowledge.dimensions()["age_group"]["total_value"]
    return scalar(db, f"SELECT mean FROM ind WHERE {REF} AND age_group = ?", [total_value])


# --- structural: the files are internally consistent ------------------------


def test_every_concept_has_the_required_fields():
    for name, concept in knowledge.concepts().items():
        assert concept.get("aliases"), f"{name} has no aliases, so search can never find it"
        assert concept.get("what_it_measures"), f"{name} has no definition"
        assert "notes" in concept, f"{name} has no notes; the caveats are the point of the layer"


def test_no_alias_is_claimed_by_two_concepts():
    """A duplicated alias makes search ranking arbitrary between the two."""
    seen: dict[str, str] = {}
    for name, concept in knowledge.concepts().items():
        for alias in concept["aliases"]:
            key = alias.lower().strip()
            assert key not in seen, f"alias {alias!r} claimed by both {seen.get(key)} and {name}"
            seen[key] = name


def test_age_aliases_reference_real_age_groups(db: duckdb.DuckDBPyConnection):
    groups = {record[0] for record in db.sql("SELECT DISTINCT age_group FROM ind").fetchall()}
    unknown = sorted(set(knowledge.age_aliases()) - groups)
    assert not unknown, f"aliases given for age groups not in the data: {unknown}"


def test_age_alias_ambiguity_is_preserved(db: duckdb.DuckDBPyConnection):
    """Ambiguous age aliases are deliberate, and a tidy-up would break resolution.

    "adults" really is both 15-49 (the prevalence convention) and 15+ (the
    treatment convention). Search is meant to return both and let the caller
    choose, so this asserts the duplication survives rather than flagging it.
    """
    assert sorted(knowledge.age_alias_index()["adults"]) == ["Y015_049", "Y015_999"]


def test_no_age_alias_names_the_same_group_twice(db: duckdb.DuckDBPyConnection):
    """A repeat would weight one candidate twice over in a ranked result."""
    for phrase, age_groups in knowledge.age_alias_index().items():
        assert len(age_groups) == len(set(age_groups)), f"{phrase!r} lists a group more than once"


def test_concept_default_age_groups_resolve(dataset_indicators: set[str], db: duckdb.DuckDBPyConnection):
    """A concept's default age_group is either a partition name or a real age group."""
    partitions = set(knowledge.age_partitions())
    groups = {record[0] for record in db.sql("SELECT DISTINCT age_group FROM ind").fetchall()}
    for name, concept in knowledge.concepts().items():
        default = concept.get("default_disaggregation", {}).get("age_group")
        if default is None:
            continue
        assert default in partitions or default in groups, f"{name} defaults to unknown age_group {default!r}"


def test_related_concepts_resolve():
    names = set(knowledge.concepts())
    for name, concept in knowledge.concepts().items():
        for related in concept.get("related", []):
            assert related in names, f"{name} points at unknown concept {related!r}"


def test_unanswerable_concepts_offer_no_indicators():
    """A concept marked unanswerable must not hand the model something to query anyway."""
    for name, concept in knowledge.concepts().items():
        if concept.get("answerable") is False:
            assert not concept["indicators"], f"{name} is unanswerable but lists indicators"


# --- the knowledge files match the data -------------------------------------


def test_concepts_only_reference_real_indicators(dataset_indicators: set[str]):
    for name, concept in knowledge.concepts().items():
        for entry in concept["indicators"]:
            assert entry["id"] in dataset_indicators, f"{name} references missing indicator {entry['id']!r}"


def test_indicators_yaml_covers_the_dataset_exactly(dataset_indicators: set[str]):
    """Every indicator needs a unit; an undocumented one would be served unitless."""
    documented = set(knowledge.indicators())
    assert documented == dataset_indicators, (
        f"undocumented: {sorted(dataset_indicators - documented)}; stale: {sorted(documented - dataset_indicators)}"
    )


@pytest.mark.parametrize("partition", sorted(knowledge.age_partitions()))
def test_each_age_partition_sums_to_its_declared_total(db: duckdb.DuckDBPyConnection, partition: str):
    """The central claim of the knowledge layer: these sets may safely be summed.

    A partition need not tile the whole population - the bands under 15 tile
    Y000_014 - so each is checked against the total it declares.
    """
    spec = knowledge.age_partitions()[partition]
    groups = spec["values"]
    placeholders = ", ".join("?" for _ in groups)
    summed = scalar(db, f"SELECT sum(mean) FROM ind WHERE {REF} AND age_group IN ({placeholders})", groups)
    expected = scalar(db, f"SELECT mean FROM ind WHERE {REF} AND age_group = ?", [spec["total"]])
    assert summed == pytest.approx(expected, rel=TOLERANCE), f"{partition} does not tile {spec['total']}"


def test_sex_both_is_the_total_not_a_third_category(db: duckdb.DuckDBPyConnection):
    where = "indicator='plhiv' AND calendar_quarter='CY2024Q3' AND area_id='MWI' AND age_group='Y000_999'"
    both = scalar(db, f"SELECT mean FROM ind WHERE {where} AND sex='both'")
    split = scalar(db, f"SELECT sum(mean) FROM ind WHERE {where} AND sex IN ('male','female')")
    assert split == pytest.approx(both, rel=TOLERANCE)


@pytest.mark.parametrize("level", [0, 1, 2, 3, 4])
def test_every_area_level_covers_the_whole_country(db: duckdb.DuckDBPyConnection, level: int):
    """Levels are nested, so each one independently sums to the national total."""
    where = "indicator='plhiv' AND calendar_quarter='CY2024Q3' AND sex='both' AND age_group='Y000_999'"
    total = scalar(db, f"SELECT sum(mean) FROM ind WHERE {where} AND area_level={level}")
    assert total == pytest.approx(all_ages_total(db), rel=TOLERANCE)


def test_latest_quarter_really_does_differ_by_indicator(db: duckdb.DuckDBPyConnection):
    """instructions.md tells the model not to assume one latest quarter for the dataset.

    If this ever becomes false, that advice can be simplified - and until then a
    caller guessing the newest quarter gets zero rows for the indicators that
    stop earlier.
    """
    latest = {record[0] for record in db.sql("SELECT max(calendar_quarter) FROM ind GROUP BY indicator").fetchall()}
    assert len(latest) > 1


def test_proportion_indicators_are_fractions_not_percentages(db: duckdb.DuckDBPyConnection):
    """A proportion drifting outside 0-1 means the unit label is wrong by 100x."""
    for name, spec in knowledge.indicators().items():
        if spec["unit"] != "proportion":
            continue
        low, high = row(db, "SELECT min(mean), max(mean) FROM ind WHERE indicator=?", [name])
        assert 0 <= low <= 1 and 0 <= high <= 1, f"{name} spans {low}-{high}, not a 0-1 proportion"


def test_rate_indicators_are_per_person_year(db: duckdb.DuckDBPyConnection):
    for name, spec in knowledge.indicators().items():
        if spec["unit"] != "rate_per_person_year":
            continue
        high = scalar(db, "SELECT max(mean) FROM ind WHERE indicator=?", [name])
        assert 0 <= high < 1, f"{name} peaks at {high}; a per-person-year rate should be well under 1"


def test_instructions_document_stays_small():
    """It is injected into the system prompt on every session; keep it a briefing."""
    assert len(knowledge.instructions()) < 8_000
