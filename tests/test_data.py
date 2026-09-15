from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.main import app
from app.ratelimit import limiter
from app.schema import DIMENSIONS, LABEL_COLUMNS, MEASURES

# What a row carries when nothing is hoisted: every dimension, every label that
# goes with one, and the requested measures.
ALL_COLUMNS = set(DIMENSIONS) | set(LABEL_COLUMNS) | set(MEASURES)


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
    assert set(body["data"][0]) == set(DIMENSIONS) | set(LABEL_COLUMNS) | {"mean", "lower"}


def test_columns_accepts_comma_separated(client: TestClient):
    body = client.get("/data", params={"columns": "mean,lower"}).json()
    assert set(body["data"][0]) == set(DIMENSIONS) | set(LABEL_COLUMNS) | {"mean", "lower"}


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
    body = client.get("/data", params={"country": "ZZZ"}).json()
    assert (body["total"], body["data"]) == (0, [])


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
    assert response.json() == {"meta": {}, "total": 0, "data": []}


# --- meta: hoisting constant dimensions out of the rows ----------------------


def test_pinned_dimensions_are_hoisted_into_meta(client: TestClient):
    params = {"country": "MWI", "indicator": "prevalence", "age_group": "Y015_049", "columns": "mean"}
    body = client.get("/data", params=params).json()

    assert body["meta"]["country"] == {"id": "MWI", "label": "Malawi"}
    assert body["meta"]["indicator"]["id"] == "prevalence"
    assert body["meta"]["age_group"] == {"id": "Y015_049", "label": "15-49"}
    # ...and are gone from the rows, along with the labels that travel with them.
    assert "indicator" not in body["data"][0]
    assert "age_group_label" not in body["data"][0]


def test_varying_dimensions_stay_in_rows_with_their_labels(client: TestClient):
    body = client.get("/data", params={"country": "MWI", "columns": "mean"}).json()
    assert "area_id" not in body["meta"]
    assert {"area_id", "area_name"} <= set(body["data"][0])
    assert body["data"][0]["area_name"] == "Malawi"


def test_meta_carries_unit_and_basis_from_the_knowledge_layer(client: TestClient):
    # Neither is in the model output; without them 0.108 could be 10.8% or 0.108%.
    body = client.get("/data", params={"country": "MWI", "indicator": "prevalence"}).json()
    assert body["meta"]["indicator"]["unit"] == "proportion"
    assert body["meta"]["indicator"]["basis"] == "residents"


def test_meta_carries_provenance_from_the_manifest(client: TestClient):
    # The country's display name and the model run both come from the manifest,
    # not from any column on the fact table.
    body = client.get("/data", params={"country": "MWI"}).json()
    assert body["meta"]["source"] == "Naomi 2.10.18 model output (mwi.zip)"
    assert body["meta"]["country"]["label"] == "Malawi"


def test_meta_is_populated_even_when_nothing_matched(client: TestClient):
    # The labels come from the dimension tables here, since there is no row to
    # read them off - and this is exactly when the caller needs telling what they
    # actually asked for.
    params = {"country": "MWI", "indicator": "art_coverage", "calendar_quarter": "CY2024Q3"}
    body = client.get("/data", params=params).json()
    assert body["total"] == 0
    assert body["meta"]["indicator"] == {
        "id": "art_coverage",
        "label": "ART coverage",
        "unit": "proportion",
        "basis": "residents",
    }
    assert body["meta"]["calendar_quarter"] == {"id": "CY2024Q3", "label": "September 2024"}


def test_a_dimension_constant_on_one_page_only_is_not_hoisted(client: TestClient):
    """Hoisting off a partial page would mislabel every later page."""
    # Page one is entirely MWI/prevalence, but the unpaged result also holds ZWE.
    body = client.get("/data", params={"limit": 2}).json()
    assert body["total"] == 4
    assert [row["country"] for row in body["data"]] == ["MWI", "MWI"]
    assert "country" not in body["meta"]
    assert "indicator" not in body["meta"]


def test_a_dimension_constant_across_a_complete_result_is_hoisted(client: TestClient):
    # Nothing pinned the indicator, but every matching row agrees and the page is
    # the whole result, so it is provably constant.
    body = client.get("/data", params={"country": "MWI", "limit": 100}).json()
    assert body["total"] == 3
    assert body["meta"]["indicator"]["id"] == "prevalence"


def test_rows_are_ordered_by_the_dimension_sort_keys(client: TestClient):
    # Ordering comes from the dimension tables' sort keys rather than the codes.
    # Age bands are the case that matters: a code sort drops 'Y000_999' (all ages)
    # into the middle of the five-year bands.
    body = client.get("/data", params={"country": "MWI", "columns": "mean"}).json()
    assert [row["area_id"] for row in body["data"]] == ["MWI", "MWI_1_1", "MWI_1_1"]


# --- age_partition: safe breakdowns without enumerating codes ----------------


def test_age_partition_expands_to_its_age_groups(client: TestClient):
    body = client.get("/data", params={"country": "MWI", "age_partition": "child_adult"}).json()
    assert body["meta"]["age_partition"] == {
        "id": "child_adult",
        "label": "children and adults",
        "tiles": "Y000_999",
    }
    # The fixture only carries Y015_049 rows, so expansion is visible in the
    # filter rather than the result: a partition that excludes it matches nothing.
    assert body["total"] == 0


def test_age_partition_selects_only_its_own_groups(client: TestClient):
    """coarse_three is not defined; children tiles Y000_014, which the fixture lacks."""
    body = client.get("/data", params={"country": "MWI", "age_partition": "children"}).json()
    assert body["total"] == 0
    assert body["meta"]["age_partition"]["tiles"] == "Y000_014"


def test_unknown_age_partition_is_rejected_with_the_valid_names(client: TestClient):
    response = client.get("/data", params={"age_partition": "ten_year_bands"})
    assert response.status_code == 422
    assert "five_year_bands" in response.json()["detail"]


def test_age_partition_and_age_group_are_mutually_exclusive(client: TestClient):
    response = client.get("/data", params={"age_partition": "child_adult", "age_group": "Y015_049"})
    assert response.status_code == 422
