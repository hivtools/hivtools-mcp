import json
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter

# The fact table as data-prep writes it: dimensions, measures, then the labels and
# sort keys denormalised on from the dimension tables.
FACT_DDL = """
    country VARCHAR, area_level BIGINT, area_id VARCHAR, sex VARCHAR,
    age_group VARCHAR, calendar_quarter VARCHAR, indicator VARCHAR,
    mean DOUBLE, se DOUBLE, median DOUBLE, mode DOUBLE, lower DOUBLE, upper DOUBLE,
    area_name VARCHAR, area_level_label VARCHAR, age_group_label VARCHAR,
    quarter_label VARCHAR, indicator_label VARCHAR,
    area_sort_order BIGINT, age_group_sort_order BIGINT,
    source VARCHAR, risk_group VARCHAR, risk_group_label VARCHAR, risk_group_sort_order BIGINT
"""
# Every Naomi and Spectrum row is for the whole population.
ALL = ("all", "All", 0)
FACT_ROWS = [
    (
        "MWI",
        0,
        "MWI",
        "both",
        "Y015_049",
        "CY2020Q3",
        "prevalence",
        0.123456789012345,
        0.010,
        0.10,
        0.10,
        0.09,
        0.11,
        "Malawi",
        "Country",
        "15-49",
        "September 2020",
        "HIV prevalence",
        1,
        1,
        "naomi",
        *ALL,
    ),
    (
        "MWI",
        1,
        "MWI_1_1",
        "female",
        "Y015_049",
        "CY2020Q3",
        "prevalence",
        0.20,
        0.020,
        0.20,
        0.20,
        0.18,
        0.22,
        "Northern",
        "Region",
        "15-49",
        "September 2020",
        "HIV prevalence",
        2,
        1,
        "naomi",
        *ALL,
    ),
    (
        "MWI",
        1,
        "MWI_1_1",
        "male",
        "Y015_049",
        "CY2020Q3",
        "prevalence",
        0.15,
        0.015,
        0.15,
        0.15,
        0.13,
        0.17,
        "Northern",
        "Region",
        "15-49",
        "September 2020",
        "HIV prevalence",
        2,
        1,
        "naomi",
        *ALL,
    ),
    (
        "ZWE",
        0,
        "ZWE",
        "both",
        "Y050_999",
        "CY2021Q1",
        "incidence",
        0.30,
        0.030,
        0.30,
        0.30,
        0.27,
        0.33,
        "Zimbabwe",
        "Country",
        "50+",
        "March 2021",
        "HIV incidence",
        1,
        4,
        "naomi",
        *ALL,
    ),
    # Tanzania has all three sources. Naomi and Spectrum both estimate national
    # PLHIV for the same quarter - the same indicator from two models - and SHIPP
    # splits adults by risk group, which differ by sex.
    (
        "TZA",
        0,
        "TZA",
        "both",
        "Y015_049",
        "CY2025Q4",
        "plhiv",
        1010.0,
        50.0,
        1010.0,
        1010.0,
        900.0,
        1100.0,
        "Tanzania",
        "Country",
        "15-49",
        "December 2025",
        "PLHIV",
        1,
        1,
        "naomi",
        *ALL,
    ),
    (
        "TZA",
        0,
        "TZA",
        "both",
        "Y015_049",
        "CY2025Q4",
        "plhiv",
        1000.0,
        None,
        None,
        None,
        None,
        None,
        "Tanzania",
        "Country",
        "15-49",
        "December 2025",
        "PLHIV",
        1,
        1,
        "spectrum",
        *ALL,
    ),
    (
        "TZA",
        0,
        "TZA",
        "female",
        "Y015_049",
        "CY2024Q4",
        "population",
        200.0,
        None,
        None,
        None,
        None,
        None,
        "Tanzania",
        "Country",
        "15-49",
        "December 2024",
        "Population",
        1,
        1,
        "shipp",
        "sexpaid12m",
        "Female sex workers",
        4,
    ),
    (
        "TZA",
        0,
        "TZA",
        "male",
        "Y015_049",
        "CY2024Q4",
        "population",
        100.0,
        None,
        None,
        None,
        None,
        None,
        "Tanzania",
        "Country",
        "15-49",
        "December 2024",
        "Population",
        1,
        1,
        "shipp",
        "male_key_pop",
        "Men who have sex with men or inject drugs",
        5,
    ),
]

# Like the facts, every dimension table is partitioned by country and source.
# Spectrum and SHIPP reuse Naomi's areas and age groups, so they only add periods,
# indicators and risk groups.
DIMS: dict[str, tuple[str, list[tuple]]] = {
    "dim_area": (
        "country VARCHAR, source VARCHAR, area_id VARCHAR, area_name VARCHAR, area_level BIGINT,"
        " area_level_label VARCHAR, parent_area_id VARCHAR, area_sort_order BIGINT",
        [
            ("MWI", "naomi", "MWI", "Malawi", 0, "Country", None, 1),
            ("MWI", "naomi", "MWI_1_1", "Northern", 1, "Region", "MWI", 2),
            ("MWI", "naomi", "MWI_3_1", "Blantyre", 3, "District", "MWI_1_1", 3),
            ("MWI", "naomi", "MWI_4_1", "Blantyre", 4, "District + Metro", "MWI_3_1", 4),
            ("MWI", "naomi", "MWI_4_2", "Blantyre City", 4, "District + Metro", "MWI_3_1", 5),
            ("ZWE", "naomi", "ZWE", "Zimbabwe", 0, "Country", None, 1),
            ("TZA", "naomi", "TZA", "Tanzania", 0, "Country", None, 1),
        ],
    ),
    "dim_age_group": (
        "country VARCHAR, source VARCHAR, age_group VARCHAR, age_group_label VARCHAR, age_group_sort_order BIGINT",
        [
            ("MWI", "naomi", "Y000_014", "0-14", 7),
            ("MWI", "naomi", "Y015_049", "15-49", 1),
            ("MWI", "naomi", "Y015_999", "15+", 3),
            ("MWI", "naomi", "Y000_999", "all ages", 5),
            ("ZWE", "naomi", "Y050_999", "50+", 4),
            ("TZA", "naomi", "Y015_049", "15-49", 1),
        ],
    ),
    "dim_period": (
        "country VARCHAR, source VARCHAR, calendar_quarter VARCHAR, quarter_label VARCHAR",
        [
            ("MWI", "naomi", "CY2020Q3", "September 2020"),
            ("MWI", "naomi", "CY2024Q3", "September 2024"),
            ("ZWE", "naomi", "CY2021Q1", "March 2021"),
            ("TZA", "naomi", "CY2025Q4", "December 2025"),
            ("TZA", "spectrum", "CY2025Q4", "December 2025"),
            ("TZA", "shipp", "CY2024Q4", "December 2024"),
        ],
    ),
    "dim_indicator": (
        "country VARCHAR, source VARCHAR, indicator VARCHAR, indicator_label VARCHAR, description VARCHAR",
        [
            ("MWI", "naomi", "prevalence", "HIV prevalence", "Proportion of total population HIV positive"),
            ("MWI", "naomi", "art_coverage", "ART coverage", "Proportion of PLHIV on ART (residents)"),
            ("ZWE", "naomi", "incidence", "HIV incidence", "HIV incidence rate per year"),
            ("TZA", "naomi", "plhiv", "PLHIV", "Number of people living with HIV"),
            ("TZA", "spectrum", "plhiv", "PLHIV", "Number of people living with HIV"),
            ("TZA", "shipp", "population", "Population", "Population size"),
        ],
    ),
    "dim_risk_group": (
        "country VARCHAR, source VARCHAR, risk_group VARCHAR, risk_group_label VARCHAR, risk_group_sort_order BIGINT",
        [
            ("MWI", "naomi", *ALL),
            ("ZWE", "naomi", *ALL),
            ("TZA", "naomi", *ALL),
            ("TZA", "spectrum", *ALL),
            ("TZA", "shipp", "sexpaid12m", "Female sex workers", 4),
            ("TZA", "shipp", "male_key_pop", "Men who have sex with men or inject drugs", 5),
            # No facts behind this one - tests that a risk group without rows is not offered.
            ("TZA", "shipp", "sexnonreg", "Non-regular sexual partner(s)", 3),
        ],
    ),
}


def _source(description: str, file: str, **extra: str) -> dict[str, str]:
    return {"description": description, "file": file, "sha256": "0" * 64, **extra}


def _country(label: str, sources: dict[str, dict[str, str]]) -> dict:
    return {
        "country_label": label,
        "label": label,
        "is_demo": False,
        "extracted_at": "2026-01-01T00:00:00+00:00",
        "sources": sources,
    }


MANIFEST = {
    "countries": {
        "MWI": _country(
            "Malawi",
            {"naomi": _source("Malawi - Naomi 2.10.18 subnational estimates", "mwi.zip", naomi_version="2.10.18")},
        ),
        "ZWE": _country(
            "Zimbabwe",
            {"naomi": _source("Zimbabwe - Naomi 2.10.18 subnational estimates", "zwe.zip", naomi_version="2.10.18")},
        ),
        "TZA": _country(
            "Tanzania",
            {
                "naomi": _source("Tanzania estimates 2026 - Naomi subnational estimates", "tza.zip"),
                "spectrum": _source("Tanzania estimates 2026 - Spectrum national projection", "tza.pjnz"),
                "shipp": _source("Tanzania estimates 2026 - SHIPP estimates by behavioural risk group", "tza.xlsx"),
            },
        ),
    }
}


def _write_table(connection: duckdb.DuckDBPyConnection, target: Path, ddl: str, rows: list[tuple]) -> None:
    placeholders = ", ".join("?" for _ in rows[0])
    connection.execute(f"CREATE OR REPLACE TABLE t ({ddl})")
    connection.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
    connection.execute(
        f"COPY (SELECT * FROM t) TO '{target}' (FORMAT parquet, PARTITION_BY (country, source), OVERWRITE_OR_IGNORE)"
    )


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The dataset layout data-prep produces: facts, dimension tables, manifest."""
    target = tmp_path_factory.mktemp("naomi-data")
    connection = duckdb.connect()
    _write_table(connection, target / "facts", FACT_DDL, FACT_ROWS)
    for name, (ddl, rows) in DIMS.items():
        _write_table(connection, target / name, ddl, rows)
    connection.close()
    (target / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    return target


@pytest.fixture
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", data_dir)
    # The per-IP rate limit would trip the many-request tests (all from 127.0.0.1);
    # it has its own coverage in test_data.py.
    monkeypatch.setattr(limiter, "enabled", False)
    with TestClient(app) as test_client:
        yield test_client
