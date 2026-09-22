"""Tests for the explanation attached to an empty result.

An empty result is indistinguishable from a true negative, so these assert that
the caller is told *which* of the two it is - and, when a filter is at fault,
which filter and what would have worked instead.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module
from app.diagnostics import MAX_LISTED, _listed
from app.main import app


def diagnostic(client: TestClient, **params) -> str:
    body = client.get("/data", params=params).json()
    assert body["total"] == 0, "this case is meant to match nothing"
    return body["diagnostic"]


# --- a value that is not in the data at all ---------------------------------


def test_unknown_indicator_points_at_search(client: TestClient):
    message = diagnostic(client, country="MWI", indicator="treatment_gap")
    assert "Unknown indicator 'treatment_gap'" in message
    assert "search(field='indicator')" in message


def test_a_near_miss_is_offered_as_a_correction(client: TestClient):
    message = diagnostic(client, country="MWI", indicator="prevalance")
    assert "Did you mean" in message
    assert "prevalence (HIV prevalence)" in message


def test_unknown_value_of_a_small_dimension_lists_the_valid_ones(client: TestClient):
    # Guessing what 'women' meant is less use than naming all three values.
    message = diagnostic(client, country="MWI", sex="women")
    assert "Unknown sex 'women'" in message
    assert "both, female, male" in message


def test_unknown_area_suggests_by_name(client: TestClient):
    message = diagnostic(client, country="MWI", area_id="MWI_9_9")
    assert "Unknown area_id" in message
    assert "search(field='area')" in message


# --- every value valid, combination empty -----------------------------------


def test_an_empty_combination_names_the_filter_and_the_values_that_work(client: TestClient):
    # CY2021Q1 exists, but only for ZWE; MWI exists, but not in that quarter.
    message = diagnostic(client, country="MWI", calendar_quarter="CY2021Q1")
    assert "No rows matched." in message
    assert "would return 1 row." in message, "singular, not '1 rows'"
    assert "ZWE" in message
    assert "you asked for MWI" in message


def test_the_narrowest_blocking_filter_is_reported_first(client: TestClient):
    """Both country and calendar_quarter would unblock it; country leaves least.

    Reporting every blocker buries the likely mistake in a list of alternatives.
    """
    message = diagnostic(client, country="MWI", calendar_quarter="CY2021Q1")
    assert message.index("Dropping country") < message.index("also return rows")
    assert "Relaxing calendar_quarter would also return rows" in message


def test_a_defined_code_with_no_rows_is_not_called_unknown(client: TestClient):
    """Y000_999 is in the age-group codelist but has no rows in the fixture.

    Treating it as unknown once made the message suggest the value as a
    correction for itself.
    """
    message = diagnostic(client, country="MWI", age_group="Y000_999")
    assert "Unknown" not in message
    assert "No rows matched." in message
    assert "age_group can be: Y015_049" in message


# --- when not to say anything -----------------------------------------------


def test_a_successful_query_carries_no_diagnostic(client: TestClient):
    body = client.get("/data", params={"country": "MWI"}).json()
    assert body["total"] == 3
    assert "diagnostic" not in body, "only the empty path should pay for, or carry, this"


def test_an_empty_dataset_says_so_rather_than_blaming_a_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings_module.settings, "naomi_data_dir", tmp_path)
    with TestClient(app) as client:
        body = client.get("/data").json()
    assert body["diagnostic"] == "The dataset is empty - no data has been loaded."


# --- source and risk_group ----------------------------------------------------


def test_unknown_source_lists_the_valid_ones(client: TestClient):
    message = diagnostic(client, country="TZA", source="unaids")
    assert "Unknown source 'unaids'" in message
    assert "naomi, shipp, spectrum" in message


def test_unknown_risk_group_is_corrected_by_name(client: TestClient):
    message = diagnostic(client, country="TZA", risk_group="sex workers")
    assert "Did you mean: sexpaid12m (Female sex workers)" in message


def test_asking_the_wrong_source_for_a_risk_group_names_the_right_one(client: TestClient):
    message = diagnostic(client, country="TZA", source="naomi", risk_group="male_key_pop", indicator="population")
    assert "Dropping source" in message
    assert "source can be: shipp" in message


def test_a_long_list_of_values_shows_both_ends():
    """Sorted quarters start in 1970; the recent end is the one usually wanted."""
    values = [f"CY{year}Q4" for year in range(1970, 2031)]
    listed = _listed(values)
    assert listed.startswith("CY1970Q4, ")
    assert listed.endswith(", CY2030Q4")
    assert ", ..., " in listed
    assert len(listed.split(", ")) == MAX_LISTED + 1
