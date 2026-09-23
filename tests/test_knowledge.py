"""Checks the hand-authored knowledge layer against the real Naomi dataset.

The files in ``app/knowledge`` make factual claims about the data - which
indicators exist, which age groups form a partition, which indicators can go
negative. Prose that has drifted from the data is worse than no prose, because
the model believes it. Every claim is therefore asserted here against the
demo zip that ships with the repo (about one second to load, so these run
alongside everything else rather than behind a marker).

Claims about Spectrum and SHIPP can only be checked against a dataset built
with them, and the only such inputs are private. The last section runs against
the locally built dataset (``make data`` with ``data-prep/private-data``
checked out) and skips when it has none - as it always does in CI - so run the
suite locally after changing the knowledge files or the extractors.
"""

import os
import re
import zipfile
from pathlib import Path

import duckdb
import pytest

from app.knowledge import loader as knowledge
from app.schema import SOURCES

ROOT = Path(__file__).parent.parent
ZIP = ROOT / "data-prep" / "raw-data" / "naomi_output_malawi.zip"
BUILT = Path(os.environ.get("HIVTOOLS_MCP_NAOMI_DATA_DIR", ROOT / "data-prep" / "naomi-data"))

# Indicators Naomi does not produce, so the demo zip cannot vouch for them. Each
# is added by data-prep/extract_spectrum_shipp.R; the built-dataset tests check
# this list against what that actually writes.
NON_NAOMI_INDICATORS = {
    "aids_deaths",
    "non_aids_deaths",
    "pmtct_receiving",
    "pmtct_need",
    "vls_suppression_prop",
    # Derived from Spectrum's counts (see SPECTRUM_DERIVED_INDICATORS in the R
    # extractor). prevalence, art_coverage and incidence are derived there too,
    # but Naomi produces those natively, so only this one is new.
    "aids_mortality_rate",
}

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
        assert concept.get("label"), f"{name} has no label; search would show a mangled key"
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
    known = dataset_indicators | NON_NAOMI_INDICATORS
    for name, concept in knowledge.concepts().items():
        for entry in concept["indicators"]:
            assert entry["id"] in known, f"{name} references missing indicator {entry['id']!r}"


def test_concepts_only_name_real_sources():
    for name, concept in knowledge.concepts().items():
        named = [entry["source"] for entry in concept["indicators"] if "source" in entry]
        default = concept.get("default_disaggregation", {}).get("source")
        for source in [*named, *([default] if default else [])]:
            assert source in SOURCES, f"{name} names unknown source {source!r}"


def test_a_concept_querying_several_sources_says_which(dataset_indicators: set[str]):
    """An indicator in more than one source is ambiguous unless the concept picks one.

    Either each indicator names its source, or the concept's default does.
    """
    for name, concept in knowledge.concepts().items():
        default = concept.get("default_disaggregation", {}).get("source")
        for entry in concept["indicators"]:
            assert entry.get("source") or default, f"{name}: {entry['id']} has no source and no default"


def test_indicators_yaml_covers_the_dataset_exactly(dataset_indicators: set[str]):
    """Every indicator needs a unit; an undocumented one would be served unitless."""
    documented = set(knowledge.indicators())
    expected = dataset_indicators | NON_NAOMI_INDICATORS
    assert documented == expected, (
        f"undocumented: {sorted(expected - documented)}; stale: {sorted(documented - expected)}"
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


def test_proportion_indicators_are_fractions_not_percentages(
    db: duckdb.DuckDBPyConnection, dataset_indicators: set[str]
):
    """A proportion drifting outside 0-1 means the unit label is wrong by 100x.

    Only checks indicators the demo zip actually has - non-Naomi indicators
    (see NON_NAOMI_INDICATORS) are checked against the built dataset instead,
    in test_units_hold_in_every_source.
    """
    for name, spec in knowledge.indicators().items():
        if spec["unit"] != "proportion" or name not in dataset_indicators:
            continue
        low, high = row(db, "SELECT min(mean), max(mean) FROM ind WHERE indicator=?", [name])
        assert 0 <= low <= 1 and 0 <= high <= 1, f"{name} spans {low}-{high}, not a 0-1 proportion"


def test_rate_indicators_are_per_person_year(db: duckdb.DuckDBPyConnection, dataset_indicators: set[str]):
    for name, spec in knowledge.indicators().items():
        if spec["unit"] != "rate_per_person_year" or name not in dataset_indicators:
            continue
        high = scalar(db, "SELECT max(mean) FROM ind WHERE indicator=?", [name])
        assert 0 <= high < 1, f"{name} peaks at {high}; a per-person-year rate should be well under 1"


def test_instructions_only_name_real_age_groups(db: duckdb.DuckDBPyConnection):
    """Guards the drift that let instructions.md list partitions that no longer existed.

    Any age-group code written into the guidance must be one the data actually
    has; the model is told to use these verbatim.
    """
    groups = {record[0] for record in db.sql("SELECT DISTINCT age_group FROM ind").fetchall()}
    mentioned = set(re.findall(r"Y\d{3}_\d{3}", knowledge.instructions()))
    assert mentioned <= groups, f"instructions name age groups not in the data: {sorted(mentioned - groups)}"


def test_instructions_document_stays_small():
    """It is injected into the system prompt on every session; keep it a briefing."""
    assert len(knowledge.instructions()) < 8_000


# --- the built dataset, with Spectrum and SHIPP -----------------------------


@pytest.fixture(scope="session")
def built() -> duckdb.DuckDBPyConnection:
    """The locally built dataset, if it has anything beyond Naomi."""
    if not any((BUILT / "facts").glob("country=*/source=[!n]*/*.parquet")):
        pytest.skip(f"no Spectrum or SHIPP data built in {BUILT}; run `make data` with private data")
    connection = duckdb.connect()
    for table in ("facts", "dim_indicator", "dim_risk_group"):
        relation = connection.read_parquet(f"{BUILT}/{table}/**/*.parquet", hive_partitioning=True, union_by_name=True)
        relation.create_view(table)
    return connection


def values(db: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> set:
    return {record[0] for record in db.sql(sql, params=params or []).fetchall()}


def test_built_indicators_are_documented_exactly(built: duckdb.DuckDBPyConnection):
    present = values(built, "SELECT DISTINCT indicator FROM facts")
    documented = set(knowledge.indicators())
    assert documented == present, f"undocumented: {sorted(present - documented)}; stale: {sorted(documented - present)}"
    naomi = values(built, "SELECT DISTINCT indicator FROM facts WHERE source = 'naomi'")
    assert present - naomi == NON_NAOMI_INDICATORS


def test_built_sources_are_the_known_ones(built: duckdb.DuckDBPyConnection):
    assert values(built, "SELECT DISTINCT source FROM facts") == set(SOURCES)


# Spectrum's derived rates: numerator, denominator expression. These are stored
# so the model queries a rate instead of fetching two counts and dividing, which
# means the arithmetic has to be checked here instead of being visible per call.
SPECTRUM_RATES = [
    ("prevalence", "plhiv", "population"),
    ("art_coverage", "art_current", "plhiv"),
    # Per HIV-negative person-year, as Naomi defines incidence - not per head of
    # population, which is a different quantity under the same id.
    ("incidence", "infections", "population - plhiv"),
    ("aids_mortality_rate", "aids_deaths", "population"),
]


@pytest.mark.parametrize(("rate", "numerator", "denominator"), SPECTRUM_RATES)
def test_spectrum_rates_equal_their_ratio(
    built: duckdb.DuckDBPyConnection, rate: str, numerator: str, denominator: str
):
    """Every stored rate is exactly the ratio it claims to be, in every cell."""
    worst = built.sql(f"""
        WITH wide AS (
            SELECT country, sex, age_group, calendar_quarter,
                   max(mean) FILTER (indicator = '{rate}') AS stored,
                   max(mean) FILTER (indicator = '{numerator}') AS num,
                   max(mean) FILTER (indicator = 'plhiv') AS plhiv,
                   max(mean) FILTER (indicator = 'population') AS population,
                   max(mean) FILTER (indicator = 'art_current') AS art_current
            FROM facts WHERE source = 'spectrum' GROUP BY ALL
        )
        SELECT max(abs(stored - num / ({denominator}))) FROM wide
        WHERE stored IS NOT NULL AND ({denominator}) > 0
    """).fetchone()
    assert worst is not None
    assert worst[0] is not None, f"no {rate} rows found to check"
    assert worst[0] < 1e-12, f"{rate} differs from {numerator}/({denominator}) by up to {worst[0]}"


# Relative, not absolute: incidence is ~0.0008, so an absolute bound loose
# enough to pass would not catch anything. Tolerances are set just above the
# genuine disagreement between two independently fitted models (measured:
# prevalence 0.09%, incidence 0.28%, art_coverage 2.5%), which leaves them tight
# enough to fail on a wrong denominator - deriving incidence per head of
# population rather than per HIV-negative person-year is a ~3.4% relative error
# at adult prevalence, well above incidence's 1% bound here.
#
# art_coverage needs the looser bound because Naomi and Spectrum fit paediatric
# ART differently; the gap is confined to the under-15 age groups, and adults
# agree to well under 1%.
CROSS_MODEL_TOLERANCE = {"prevalence": 0.01, "incidence": 0.01, "art_coverage": 0.03}


@pytest.mark.parametrize(("rate", "tolerance"), sorted(CROSS_MODEL_TOLERANCE.items()))
def test_spectrum_rates_agree_with_naomi(built: duckdb.DuckDBPyConnection, rate: str, tolerance: float):
    """One id is one quantity, whichever model estimated it."""
    worst = built.sql(
        """
        SELECT max(abs(n.mean - s.mean) / n.mean)
        FROM facts n JOIN facts s
          ON n.country = s.country AND n.area_id = s.area_id AND n.sex = s.sex
         AND n.age_group = s.age_group AND n.calendar_quarter = s.calendar_quarter
         AND n.indicator = s.indicator
        WHERE n.source = 'naomi' AND s.source = 'spectrum' AND n.indicator = ?
          AND n.area_level = 0 AND n.mean > 0
        """,
        params=[rate],
    ).fetchone()
    assert worst is not None
    if worst[0] is None:
        pytest.skip(f"no overlapping national {rate} rows between Naomi and Spectrum")
    assert worst[0] < tolerance, f"{rate} differs between Naomi and Spectrum by up to {worst[0]:.1%}"


def test_concept_indicators_exist_in_the_source_they_name(built: duckdb.DuckDBPyConnection):
    pairs = set(built.sql("SELECT DISTINCT indicator, source FROM facts").fetchall())
    for name, concept in knowledge.concepts().items():
        default = concept.get("default_disaggregation", {}).get("source")
        for entry in concept["indicators"]:
            source = entry.get("source", default)
            assert (entry["id"], source) in pairs, f"{name}: {entry['id']} is not in source {source!r}"


def test_risk_group_aliases_reference_real_groups(built: duckdb.DuckDBPyConnection):
    groups = values(built, "SELECT DISTINCT risk_group FROM facts")
    unknown = sorted(set(knowledge.risk_group_aliases()) - groups)
    assert not unknown, f"aliases given for risk groups not in the data: {unknown}"
    assert "all" not in knowledge.risk_group_aliases(), "'all' is the default and is kept out of search"


def test_naomi_and_spectrum_have_only_the_whole_population(built: duckdb.DuckDBPyConnection):
    assert values(built, "SELECT DISTINCT risk_group FROM facts WHERE source <> 'shipp'") == {"all"}


def test_risk_groups_add_up_across_sexes(built: duckdb.DuckDBPyConnection):
    """dimensions.yaml: under sex=both the groups sum to the two sexes' groups together."""
    rows = built.sql("""
        SELECT country, sex, sum(mean) FROM facts
        WHERE source = 'shipp' AND indicator = 'population' AND area_level = 0 AND age_group = 'Y015_049'
        GROUP BY ALL
    """).fetchall()
    totals: dict[str, dict[str, float]] = {}
    for country, sex, total in rows:
        totals.setdefault(country, {})[sex] = total
    for country, by_sex in totals.items():
        assert by_sex["both"] == pytest.approx(by_sex["female"] + by_sex["male"], rel=1e-9), country


def test_single_sex_risk_groups_are_in_one_sex_only(built: duckdb.DuckDBPyConnection):
    """The knowledge files say female sex workers are women and male_key_pop (MSM/PWID) men."""
    sexes = dict(
        built.sql("""
            SELECT risk_group, list_sort(list_distinct(list(sex))) FROM facts
            WHERE source = 'shipp' GROUP BY 1
        """).fetchall()
    )
    assert sexes["sexpaid12m"] == ["both", "female"]
    assert sexes["male_key_pop"] == ["both", "male"]
    for shared in ("nosex12m", "sexcohab", "sexnonreg"):
        assert sexes[shared] == ["both", "female", "male"]


@pytest.mark.parametrize(
    ("source", "risk_group"), [("spectrum", "all"), ("shipp", "sexcohab"), ("shipp", "male_key_pop")]
)
def test_derived_sources_keep_the_additivity_rules(built: duckdb.DuckDBPyConnection, source: str, risk_group: str):
    """The Naomi rules - sex, area level, age partitions - hold for the sources data-prep aggregates."""
    indicator = "plhiv"
    quarter = built.sql("SELECT max(calendar_quarter) FROM facts WHERE source = ?", params=[source]).fetchone()
    assert quarter is not None
    where = "source = ? AND indicator = ? AND calendar_quarter = ? AND risk_group = ?"
    params = [source, indicator, quarter[0], risk_group]
    country = built.sql(f"SELECT min(country) FROM facts WHERE {where}", params=params).fetchone()
    assert country is not None
    base = f"{where} AND country = ?"
    params = [*params, country[0]]

    def total(extra: str, extra_params: list) -> float:
        return scalar(built, f"SELECT sum(mean) FROM facts WHERE {base} AND {extra}", [*params, *extra_params])

    age = "Y015_049"
    national = total("area_level = 0 AND sex = 'both' AND age_group = ?", [age])
    assert total("area_level = 0 AND sex IN ('male', 'female') AND age_group = ?", [age]) == pytest.approx(national)
    for level in values(built, f"SELECT DISTINCT area_level FROM facts WHERE {base}", params):
        by_level = total("area_level = ? AND sex = 'both' AND age_group = ?", [level, age])
        assert by_level == pytest.approx(national), f"level {level}"
    for name, spec in knowledge.age_partitions().items():
        groups = spec["values"]
        if values(built, f"SELECT DISTINCT age_group FROM facts WHERE {base}", params) >= {*groups, spec["total"]}:
            placeholders = ", ".join("?" for _ in groups)
            summed = total(f"area_level = 0 AND sex = 'both' AND age_group IN ({placeholders})", groups)
            expected = total("area_level = 0 AND sex = 'both' AND age_group = ?", [spec["total"]])
            assert summed == pytest.approx(expected), f"{name} does not tile {spec['total']} in {source}"


def test_spectrum_is_national_and_annual(built: duckdb.DuckDBPyConnection):
    """instructions.md: national only, one value a year."""
    assert values(built, "SELECT DISTINCT area_level FROM facts WHERE source = 'spectrum'") == {0}
    per_year = built.sql("""
        SELECT max(n) FROM (
            SELECT count(DISTINCT calendar_quarter) AS n FROM facts WHERE source = 'spectrum'
            GROUP BY country, substr(calendar_quarter, 3, 4)
        )
    """).fetchone()
    assert per_year == (1,)


def test_shipp_is_adults_for_one_quarter(built: duckdb.DuckDBPyConnection):
    """instructions.md: adults 15-49, one quarter."""
    ages = values(built, "SELECT DISTINCT age_group FROM facts WHERE source = 'shipp'")
    assert "Y015_049" in ages
    assert all(age[1:4] >= "015" and age[5:] <= "049" for age in ages), sorted(ages)
    quarters = built.sql(
        "SELECT max(n) FROM (SELECT count(DISTINCT calendar_quarter) n FROM facts WHERE source = 'shipp' GROUP BY country)"
    ).fetchone()
    assert quarters == (1,)


def test_units_hold_in_every_source(built: duckdb.DuckDBPyConnection):
    """Guards the unit, not the model: a percentage would run into the tens.

    Real model output can edge slightly past 1 in small populations, and
    describing that is an epidemiologist's call, not this file's.
    """
    for name, spec in knowledge.indicators().items():
        low, high = row(built, "SELECT min(mean), max(mean) FROM facts WHERE indicator = ?", [name])
        if spec["unit"] == "proportion":
            assert 0 <= low <= high < 2, f"{name} spans {low}-{high}"
        elif spec["unit"] == "rate_per_person_year":
            assert 0 <= low <= high < 1, f"{name} spans {low}-{high}"


def test_non_naomi_indicators_are_labelled(built: duckdb.DuckDBPyConnection):
    labelled = values(built, "SELECT DISTINCT indicator FROM dim_indicator WHERE indicator_label IS NOT NULL")
    assert labelled >= NON_NAOMI_INDICATORS
