"""Tests for ``/search``: turning what a user typed into IDs.

The failure this guards against is silent: a term that resolves to the wrong
thing, or to nothing, produces an empty data query rather than an error, so the
assertions here are about *which* entry wins and how much detail rides with it.
"""

import pytest
from fastapi.testclient import TestClient

from app.knowledge.loader import age_aliases, concepts, indicators, risk_group_aliases
from app.main import app
from app.search import MAX_LISTED_QUARTERS, _quarters, normalise, score_term, search_one, tokens


def alias_owners() -> list[tuple[str, str, str]]:
    """(alias, field, owner id) for every hand-authored alias in the knowledge files."""
    cases = [(a, "concept", name) for name, c in concepts().items() for a in c["aliases"]]
    cases += [(a, "indicator", i) for i, s in indicators().items() for a in s.get("aliases", [])]
    cases += [(a, "age_group", code) for code, aliases in age_aliases().items() for a in aliases]
    cases += [(a, "risk_group", code) for code, aliases in risk_group_aliases().items() for a in aliases]
    return cases


def first(client: TestClient, term: str, **params) -> dict:
    body = client.get("/search", params={"q": term, "country": "MWI", **params}).json()
    return body["results"][0]


# --- scoring ----------------------------------------------------------------


def test_token_set_match_ignores_word_order_and_stopwords():
    assert tokens("burden of HIV") == tokens("HIV burden")
    assert score_term("burden of HIV", "hiv burden") > 95


def test_normalisation_strips_case_accents_and_punctuation():
    assert normalise("Côte d'Ivoire!") == "cote d ivoire"


def test_substring_matching_respects_word_boundaries():
    """'ANC' is inside 'prevelance' as characters, but is not a word in it.

    Without the boundary check the antenatal_care concept outscored the typo's
    real target.
    """
    assert score_term("prevelance", "ANC") < 50
    assert score_term("Lilongwe district", "Lilongwe") > 80


# --- resolution -------------------------------------------------------------


def test_concept_alias_resolves_to_the_concept(client: TestClient):
    assert first(client, "gap in treatment")["results"][0]["id"] == "treatment_gap"


def test_age_alias_resolves_to_an_age_group(client: TestClient):
    top = first(client, "children")["results"][0]
    assert (top["field"], top["id"]) == ("age_group", "Y000_014")


def test_area_name_resolves_to_an_area_id(client: TestClient):
    top = first(client, "Northern")["results"][0]
    assert (top["field"], top["id"]) == ("area", "MWI_1_1")


def test_a_typo_still_finds_the_indicator(client: TestClient):
    ids = {hit["id"] for hit in first(client, "prevelance")["results"]}
    assert "prevalence" in ids


def test_no_match_returns_an_empty_result_not_an_error(client: TestClient):
    body = first(client, "quantum chromodynamics")
    assert body["results"] == []
    assert body["ambiguous"] is False


def test_every_alias_surfaces_its_own_entry(client: TestClient):
    """The whole point of the alias files is that these words resolve.

    Checked in-process rather than over HTTP: there are a couple of hundred, and
    the endpoint is rate limited. Top three rather than top one, because an alias
    may legitimately be shared - "treatment gap" names both a concept and the
    indicator behind it, and both should come back.
    """
    assert client is not None  # the fixture builds the index during lifespan
    index = app.state.search_index
    known = {(entry.field, entry.id) for entry in index}
    misses = []
    for alias, field, owner in alias_owners():
        if (field, owner) not in known:
            continue  # the fixture carries a subset of the real dataset
        # Unscoped: the fixture splits entries across two countries, and country
        # scoping has its own test. This one is about the alias reaching the index.
        hits = search_one(alias, index, None, None, 3)["results"]
        if not any(hit["field"] == field and hit["id"] == owner for hit in hits):
            misses.append((alias, f"{field}:{owner}", [f"{h['field']}:{h['id']}" for h in hits]))
    assert not misses, f"aliases that do not surface their own entry: {misses}"


def test_related_concepts_come_back_named_not_as_bare_ids(client: TestClient):
    """An id alone would cost another search just to learn what it refers to."""
    related = first(client, "treatment gap")["results"][0]["related"]
    assert {"id": "treatment_coverage", "label": "ART treatment coverage"} in related


def test_aliases_are_matcher_input_and_never_returned(client: TestClient):
    """They exist to be matched against, not read: ten per concept would be waste."""
    for hit in first(client, "treatment gap")["results"]:
        assert "aliases" not in hit


# --- detail: fat top hit, thin remainder ------------------------------------


def test_top_concept_comes_back_in_full_and_the_rest_as_summaries(client: TestClient):
    results = first(client, "treatment gap")["results"]
    assert results[0]["detail"] == "full"
    assert "notes" in results[0], "the caveats are the reason the concept layer exists"
    assert all(hit.get("detail") != "full" for hit in results[1:])


def test_a_full_concept_carries_its_indicators_with_coverage(client: TestClient):
    """Coverage is what stops the caller asking for a quarter that has no rows."""
    concept = first(client, "HIV burden", field="concept")["results"][0]
    assert concept["id"] == "hiv_burden"
    by_id = {item["id"]: item for item in concept["indicators"]}
    assert by_id["prevalence"]["unit"] == "proportion"
    assert by_id["prevalence"]["coverage"]["naomi"]["calendar_quarter"] == ["CY2020Q3"]


def test_summaries_say_there_is_more_behind_them(client: TestClient):
    """So the second call is discoverable without being mandatory."""
    results = first(client, "treatment")["results"]
    detailed = [hit for hit in results[1:] if hit["field"] in {"concept", "indicator"}]
    assert detailed and all(hit["detail"] == "summary" for hit in detailed)


# --- ambiguity --------------------------------------------------------------


def test_same_named_areas_are_flagged_ambiguous(client: TestClient):
    body = first(client, "Blantyre")
    assert body["ambiguous"] is True
    assert {hit["id"] for hit in body["results"][:2]} == {"MWI_3_1", "MWI_4_1"}
    # ...and both come back with their level, which is what tells them apart.
    assert {hit["area_level"] for hit in body["results"][:2]} == {3, 4}


def test_an_ambiguous_age_alias_is_flagged(client: TestClient):
    """'adults' is 15-49 by the prevalence convention and 15+ by the treatment one."""
    body = first(client, "adults")
    assert body["ambiguous"] is True
    assert {hit["id"] for hit in body["results"][:2]} == {"Y015_049", "Y015_999"}


def test_an_age_group_and_a_partition_of_it_are_ambiguous(client: TestClient):
    """'children' is either the 0-14 age group or a breakdown of it, and the two
    lead to different queries, so the caller has a real choice to make."""
    body = first(client, "children")
    assert body["ambiguous"] is True
    assert {hit["field"] for hit in body["results"][:2]} == {"age_group", "age_partition"}


def test_a_concept_and_its_own_indicator_are_not_ambiguous(client: TestClient):
    """Both match 'treatment gap', but the concept subsumes the indicator."""
    body = first(client, "treatment gap")
    assert body["ambiguous"] is False
    assert body["results"][0]["field"] == "concept"


# --- parameters -------------------------------------------------------------


def test_field_restricts_the_kinds_returned(client: TestClient):
    results = first(client, "treatment gap", field="indicator")["results"]
    assert results and {hit["field"] for hit in results} == {"indicator"}


def test_unknown_field_is_rejected_rather_than_silently_empty(client: TestClient):
    response = client.get("/search", params={"q": "x", "field": "indicatr"})
    assert response.status_code == 422
    assert "indicatr" in response.json()["detail"]


def test_several_terms_resolve_in_one_call(client: TestClient):
    body = client.get("/search", params=[("q", "Northern"), ("q", "children"), ("q", "treatment gap")]).json()
    assert [block["query"] for block in body["results"]] == ["Northern", "children", "treatment gap"]
    assert all(block["results"] for block in body["results"])


def test_country_scopes_area_results(client: TestClient):
    body = client.get("/search", params={"q": "Zimbabwe", "country": "MWI"}).json()["results"][0]
    assert all(hit.get("country") != "ZWE" for hit in body["results"])


def test_limit_caps_matches_per_term(client: TestClient):
    assert len(first(client, "treatment", limit=2)["results"]) <= 2


@pytest.mark.parametrize("bad", [{"limit": 0}, {"limit": 99}])
def test_limit_bounds_are_enforced(client: TestClient, bad: dict):
    assert client.get("/search", params={"q": "x", **bad}).status_code == 422


def test_query_is_required(client: TestClient):
    assert client.get("/search").status_code == 422


# --- sources and risk groups ------------------------------------------------


def test_coverage_is_keyed_by_source(client: TestClient):
    """The sources cover different ground, so coverage says what each one has."""
    hit = first(client, "PLHIV", field="indicator", country="TZA")["results"][0]
    assert hit["id"] == "plhiv"
    assert set(hit["coverage"]) == {"naomi", "spectrum"}
    assert hit["coverage"]["spectrum"]["area_level"] == [0]
    # Only a source that splits by risk group says so.
    assert "risk_group" not in hit["coverage"]["naomi"]


def test_coverage_names_the_risk_groups_a_source_has(client: TestClient):
    hit = first(client, "population", field="indicator", country="TZA")["results"][0]
    assert hit["coverage"]["shipp"]["risk_group"] == ["male_key_pop", "sexpaid12m"]


def test_a_long_run_of_quarters_is_given_as_a_span():
    quarters = [f"CY{year}Q4" for year in range(1970, 1971 + MAX_LISTED_QUARTERS)]
    assert _quarters(quarters) == {"from": "CY1970Q4", "to": quarters[-1], "count": len(quarters)}
    assert _quarters(quarters[:2]) == quarters[:2]


def test_a_risk_group_says_which_source_and_sex_have_it(client: TestClient):
    """Female sex workers are only in SHIPP's female rows; querying anything else is empty."""
    top = first(client, "sex workers", country="TZA")["results"][0]
    assert (top["field"], top["id"], top["label"]) == ("risk_group", "sexpaid12m", "Female sex workers")
    assert top["source"] == ["shipp"]
    assert top["sex"] == ["female"]


def test_a_risk_group_with_no_rows_is_not_offered(client: TestClient):
    """sexnonreg is in the fixture's risk-group table but has no facts behind it."""
    ids = {hit["id"] for hit in first(client, "casual partners", country="TZA")["results"]}
    assert "sexnonreg" not in ids


def test_the_whole_population_group_is_not_a_search_result(client: TestClient):
    """'all' is the default, and would otherwise match every phrase containing the word."""
    index = app.state.search_index
    assert client is not None
    assert not [entry for entry in index if entry.field == "risk_group" and entry.id == "all"]


def test_key_populations_resolve_to_the_concept(client: TestClient):
    top = first(client, "key populations")["results"][0]
    assert (top["field"], top["id"]) == ("concept", "key_populations")
    assert {item.get("source") for item in top["indicators"]} == {"shipp"}
