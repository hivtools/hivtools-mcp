from collections.abc import Iterator
from pathlib import Path

import duckdb
import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter

# country, area_level, area_id, sex, age_group, calendar_quarter, indicator, mean, se, median, mode, lower, upper
FIXTURE_ROWS = [
    ("MWI", 0, "MWI", "both", "Y015_049", "CY2020Q3", "prevalence", 0.123456789012345, 0.010, 0.10, 0.10, 0.09, 0.11),
    ("MWI", 1, "MWI_1_1", "female", "Y015_049", "CY2020Q3", "prevalence", 0.20, 0.020, 0.20, 0.20, 0.18, 0.22),
    ("MWI", 1, "MWI_1_1", "male", "Y015_049", "CY2020Q3", "prevalence", 0.15, 0.015, 0.15, 0.15, 0.13, 0.17),
    ("ZWE", 0, "ZWE", "both", "Y050_999", "CY2021Q1", "incidence", 0.30, 0.030, 0.30, 0.30, 0.27, 0.33),
]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A small Hive-partitioned Parquet dataset with the real column layout."""
    target = tmp_path_factory.mktemp("naomi-data")
    connection = duckdb.connect()
    connection.execute(
        """
        CREATE TABLE t (
            country VARCHAR, area_level BIGINT, area_id VARCHAR, sex VARCHAR,
            age_group VARCHAR, calendar_quarter VARCHAR, indicator VARCHAR,
            mean DOUBLE, se DOUBLE, median DOUBLE, mode DOUBLE, lower DOUBLE, upper DOUBLE
        )
        """
    )
    connection.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", FIXTURE_ROWS)
    connection.execute(
        f"COPY (SELECT * FROM t) TO '{target}' (FORMAT parquet, PARTITION_BY (country), OVERWRITE_OR_IGNORE)"
    )
    connection.close()
    return target


@pytest.fixture
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", data_dir)
    # The per-IP rate limit would trip the many-request tests (all from 127.0.0.1);
    # it has its own coverage in test_data.py.
    monkeypatch.setattr(limiter, "enabled", False)
    with TestClient(app) as test_client:
        yield test_client
