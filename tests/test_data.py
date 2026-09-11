from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter
from app.schema import DIMENSIONS, MEASURES

ALL_COLUMNS = set(DIMENSIONS) | set(MEASURES)


def test_no_filters_returns_every_row_with_every_column(client: TestClient):
    body = client.get("/data").json()
    assert body["total"] == 4
    assert len(body["data"]) == 4
    assert set(body["data"][0]) == ALL_COLUMNS


def test_filter_by_single_dimension(client: TestClient):
    assert client.get("/data", params={"country": "MWI"}).json()["total"] == 3


def test_area_level_is_coerced_to_int(client: TestClient):
    assert client.get("/data", params={"area_level": 0}).json()["total"] == 2


def test_repeated_param_is_an_or(client: TestClient):
    body = client.get("/data", params=[("indicator", "prevalence"), ("indicator", "incidence")]).json()
    assert body["total"] == 4


def test_separate_filters_are_anded(client: TestClient):
    assert client.get("/data", params={"country": "MWI", "sex": "female"}).json()["total"] == 1


def test_columns_restricts_returned_measures(client: TestClient):
    body = client.get("/data", params=[("columns", "mean"), ("columns", "lower")]).json()
    assert set(body["data"][0]) == set(DIMENSIONS) | {"mean", "lower"}


def test_columns_accepts_comma_separated(client: TestClient):
    body = client.get("/data", params={"columns": "mean,lower"}).json()
    assert set(body["data"][0]) == set(DIMENSIONS) | {"mean", "lower"}


def test_filter_accepts_comma_separated(client: TestClient):
    body = client.get("/data", params={"indicator": "prevalence,incidence"}).json()
    assert body["total"] == 4


def test_unknown_column_is_rejected(client: TestClient):
    assert client.get("/data", params={"columns": "not_a_column"}).status_code == 422
    assert client.get("/data", params={"columns": "mean,bogus"}).status_code == 422


def test_non_integer_area_level_is_rejected(client: TestClient):
    assert client.get("/data", params={"area_level": "district"}).status_code == 422


def test_data_response_is_cacheable(client: TestClient):
    response = client.get("/data", params={"limit": 1})
    assert response.headers["cache-control"] == f"public, max-age={settings_module.settings.cache_max_age}"


def test_data_is_rate_limited(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", data_dir)
    monkeypatch.setattr(limiter, "enabled", True)
    limiter.reset()
    try:
        with TestClient(app) as unthrottled_client:
            codes = [unthrottled_client.get("/data", params={"limit": 1}).status_code for _ in range(40)]
    finally:
        limiter.reset()
    assert 200 in codes
    assert 429 in codes


def test_measure_values_are_rounded_to_default_sig_figs(client: TestClient):
    row = client.get("/data", params={"country": "MWI", "area_level": "0", "sex": "both"}).json()["data"][0]
    assert row["mean"] == 0.123457  # fixture stores 0.123456789012345; default is 6 sig figs


def test_sig_figs_can_be_overridden_per_request(client: TestClient):
    params = {"country": "MWI", "area_level": "0", "sex": "both", "sig_figs": 3}
    row = client.get("/data", params=params).json()["data"][0]
    assert row["mean"] == 0.123


def test_total_is_the_full_match_count_not_the_page(client: TestClient):
    body = client.get("/data", params={"limit": 2}).json()
    assert body["total"] == 4
    assert len(body["data"]) == 2


def test_limit_and_offset_paginate(client: TestClient):
    page1 = client.get("/data", params={"limit": 2}).json()
    page2 = client.get("/data", params={"limit": 2, "offset": 2}).json()
    assert len(page1["data"]) == len(page2["data"]) == 2
    assert page1["data"] != page2["data"]


def test_limit_over_cap_is_rejected(client: TestClient):
    assert client.get("/data", params={"limit": 10_000_000}).status_code == 422


def test_filter_values_are_bound_not_interpolated(client: TestClient):
    body = client.get("/data", params={"area_id": "'; DROP TABLE indicators; --"})
    assert body.status_code == 200
    assert body.json()["total"] == 0
    # dataset still intact and queryable
    assert client.get("/data").json()["total"] == 4


def test_no_match_returns_empty(client: TestClient):
    assert client.get("/data", params={"country": "ZZZ"}).json() == {"total": 0, "data": []}


def test_concurrent_requests_are_served(client: TestClient):
    def hit(area_level: int) -> int:
        return client.get("/data", params={"area_level": area_level % 5, "limit": 10}).status_code

    with ThreadPoolExecutor(max_workers=16) as pool:
        codes = list(pool.map(hit, range(64)))
    assert codes == [200] * 64


def test_starts_and_serves_with_no_parquet_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", tmp_path)
    with TestClient(app) as client:
        response = client.get("/data")
    assert response.status_code == 200
    assert response.json() == {"total": 0, "data": []}
