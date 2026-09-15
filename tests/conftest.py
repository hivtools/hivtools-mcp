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
    area_sort_order BIGINT, age_group_sort_order BIGINT
"""
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
    ),
]

DIMS: dict[str, tuple[str, list[tuple]]] = {
    "dim_area": (
        "country VARCHAR, area_id VARCHAR, area_name VARCHAR, area_level BIGINT,"
        " area_level_label VARCHAR, parent_area_id VARCHAR, area_sort_order BIGINT",
        [
            ("MWI", "MWI", "Malawi", 0, "Country", None, 1),
            ("MWI", "MWI_1_1", "Northern", 1, "Region", "MWI", 2),
            ("MWI", "MWI_3_1", "Blantyre", 3, "District", "MWI_1_1", 3),
            ("MWI", "MWI_4_1", "Blantyre", 4, "District + Metro", "MWI_3_1", 4),
            ("MWI", "MWI_4_2", "Blantyre City", 4, "District + Metro", "MWI_3_1", 5),
            ("ZWE", "ZWE", "Zimbabwe", 0, "Country", None, 1),
        ],
    ),
    "dim_age_group": (
        "country VARCHAR, age_group VARCHAR, age_group_label VARCHAR, age_group_sort_order BIGINT",
        [
            ("MWI", "Y000_014", "0-14", 7),
            ("MWI", "Y015_049", "15-49", 1),
            ("MWI", "Y015_999", "15+", 3),
            ("MWI", "Y000_999", "all ages", 5),
            ("ZWE", "Y050_999", "50+", 4),
        ],
    ),
    "dim_period": (
        "country VARCHAR, calendar_quarter VARCHAR, quarter_label VARCHAR",
        [
            ("MWI", "CY2020Q3", "September 2020"),
            ("MWI", "CY2024Q3", "September 2024"),
            ("ZWE", "CY2021Q1", "March 2021"),
        ],
    ),
    "dim_indicator": (
        "country VARCHAR, indicator VARCHAR, indicator_label VARCHAR, description VARCHAR",
        [
            ("MWI", "prevalence", "HIV prevalence", "Proportion of total population HIV positive"),
            ("MWI", "art_coverage", "ART coverage", "Proportion of PLHIV on ART (residents)"),
            ("ZWE", "incidence", "HIV incidence", "HIV incidence rate per year"),
        ],
    ),
}

MANIFEST = {
    "countries": {
        "MWI": {
            "country_label": "Malawi",
            "source": "Naomi 2.10.18 model output (mwi.zip)",
            "is_demo": False,
            "naomi_version": "2.10.18",
            "source_file": "mwi.zip",
            "source_sha256": "0" * 64,
            "extracted_at": "2026-01-01T00:00:00+00:00",
        },
        "ZWE": {
            "country_label": "Zimbabwe",
            "source": "Naomi 2.10.18 model output (zwe.zip)",
            "is_demo": False,
            "naomi_version": "2.10.18",
            "source_file": "zwe.zip",
            "source_sha256": "1" * 64,
            "extracted_at": "2026-01-01T00:00:00+00:00",
        },
    }
}


def _write_table(connection: duckdb.DuckDBPyConnection, target: Path, ddl: str, rows: list[tuple]) -> None:
    placeholders = ", ".join("?" for _ in rows[0])
    connection.execute(f"CREATE OR REPLACE TABLE t ({ddl})")
    connection.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
    connection.execute(
        f"COPY (SELECT * FROM t) TO '{target}' (FORMAT parquet, PARTITION_BY (country), OVERWRITE_OR_IGNORE)"
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
