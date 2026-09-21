"""The ``/search`` endpoint: resolving plain-language terms to dataset IDs.

Nothing in this dataset has a guessable ID. ``untreated_plhiv_num`` is what a
user calls a "treatment gap", ``MWI_3_13_demo`` is Lilongwe, ``Y000_014`` is
"children", ``sexpaid12m`` is female sex workers. A model that guesses gets a
silently empty result, so it needs a way to look them up.

One index, not one per dimension. The whole searchable vocabulary - indicators,
areas, age groups, age partitions, risk groups, concepts and methodology - is a
few hundred rows per country, so it all goes in one list with a ``field`` tag, and ``field``
is an optional filter rather than a required argument. That matters because the caller
often does not know which dimension a word belongs to: "all ages" could be an age
group or a concept, "district" is an area level, "children" is an age group.

Matching is layered and deliberately dependency-free: exact match, then token-set
comparison (which makes "burden of HIV" and "HIV burden" identical once stopwords
go), then substring, then difflib as a last resort for typos. At a few hundred
entries this is microseconds and every score is explainable. If quality turns out
to be the limit, the upgrade path is rapidfuzz for the fuzzy tier and DuckDB's
FTS extension for BM25 over labels and descriptions - neither is worth a
dependency yet.

Results are fat for the hit the caller will actually use and thin for the rest.
A concept's ``notes`` field - "the count form and the coverage form rank
districts differently, report both" - is the entire reason the concept layer
exists, and if it sat behind an optional second call the model would skip it.
Measured on this dataset, returning the top hit in full plus four summaries also
costs fewer tokens *and* one round trip fewer than returning summaries and
fetching the winner.
"""

import difflib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.knowledge.loader import (
    age_aliases,
    age_partitions,
    concepts,
    indicators,
    methodology,
    methodology_instruction,
    risk_group_aliases,
)
from app.observability import UserQuestion
from app.ratelimit import limiter
from app.schema import FACT_VIEW
from app.settings import settings

router = APIRouter()

# Dropped before token comparison so word order and connectives stop mattering:
# "burden of HIV" and "HIV burden" become the same token set.
STOPWORDS = frozenset({"a", "an", "and", "at", "by", "for", "in", "of", "on", "the", "to", "with"})

# These carry knowledge worth withholding until it is needed; an area or an age
# group is small enough that there is no fat version to hold back.
DETAILED_FIELDS = frozenset({"concept", "methodology", "indicator"})

FIELDS = ("concept", "methodology", "indicator", "area", "age_group", "age_partition", "risk_group")

# Ties are broken towards the kind of match that tells the caller most. A concept
# subsumes the indicators it names, so when both match a phrase equally well -
# "treatment gap" is a concept and an alias of untreated_plhiv_num - the concept
# is the more useful answer, not a competing one.
FIELD_PRIORITY = {
    "concept": 0,
    "methodology": 1,
    "indicator": 2,
    "age_group": 3,
    "age_partition": 4,
    "risk_group": 5,
    "area": 6,
}

# A result is ambiguous when the runner-up is nearly as good as the winner and
# both are real candidates: "Lilongwe" the district vs the district+metro area,
# "adults" as 15-49 vs 15+, "children" as the 0-14 age group vs a breakdown of it.
# The exception is a runner-up the winner already accounts for (see _subsumes).
AMBIGUITY_MARGIN = 6.0
AMBIGUITY_FLOOR = 85.0
MATCH_FLOOR = 55.0
MAX_DETAILED = 2

# Spectrum has a value for every year since 1970. Listing them all in every
# search result costs tokens and says nothing a span does not.
MAX_LISTED_QUARTERS = 8

# The whole-population risk group every Naomi and Spectrum row has. Never worth
# resolving - it is what a query gets by default - and "all" would otherwise
# match every phrase containing the word.
WHOLE_POPULATION = "all"


def normalise(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    stripped = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(re.sub(r"[^a-z0-9\s]+", " ", stripped.lower()).split())


def tokens(text: str) -> frozenset[str]:
    return frozenset(word for word in normalise(text).split() if word not in STOPWORDS)


def score_term(query: str, term: str) -> float:
    """How well one search term matches one of an entry's names, 0-100."""
    left, right = normalise(query), normalise(term)
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0

    best = 0.0
    query_tokens, term_tokens = tokens(query), tokens(term)
    if query_tokens and term_tokens:
        if query_tokens == term_tokens:
            best = 97.0
        elif query_tokens <= term_tokens or term_tokens <= query_tokens:
            # One is a refinement of the other: "prevalence" vs "HIV prevalence".
            best = 90.0
        else:
            overlap = len(query_tokens & term_tokens) / len(query_tokens | term_tokens)
            best = 55.0 + 35.0 * overlap if overlap else 0.0
    shorter, longer = sorted((left, right), key=len)
    if re.search(rf"\b{re.escape(shorter)}\b", longer):
        best = max(best, 82.0)
    ratio = difflib.SequenceMatcher(None, left, right).ratio()
    if ratio > 0.75:
        best = max(best, ratio * 88.0)
    return best


@dataclass(frozen=True)
class Entry:
    """One searchable thing: what it is, what it is called, and its detail."""

    field: str
    id: str
    label: str
    description: str | None = None
    country: str | None = None
    terms: tuple[str, ...] = ()
    detail: dict[str, Any] = dataclass_field(default_factory=dict)

    def score(self, query: str) -> float:
        return max((score_term(query, term) for term in self.terms), default=0.0)


def _rows(connection: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    try:
        return connection.sql(sql).fetchall()
    except duckdb.Error:  # pragma: no cover - a missing view means an empty dataset
        return []


def _quarters(quarters: list[str]) -> list[str] | dict[str, Any]:
    """Every quarter covered, or the span when there are too many to list."""
    if len(quarters) <= MAX_LISTED_QUARTERS:
        return quarters
    return {"from": quarters[0], "to": quarters[-1], "count": len(quarters)}


def _coverage(connection: duckdb.DuckDBPyConnection) -> dict[tuple[str, str], dict[str, Any]]:
    """Which dimension values each indicator actually has rows for, per country and source.

    This is the field that prevents the commonest silent failure: asking for an
    indicator in a quarter it does not cover and getting zero rows with no hint.
    It is split by source because the sources cover very different ground - the
    same indicator can be subnational for a few quarters from Naomi and national
    for sixty years from Spectrum.
    """
    sql = f"""
        SELECT country, indicator, source,
               list_sort(list_distinct(list(calendar_quarter))) AS quarters,
               list_sort(list_distinct(list(sex))) AS sexes,
               list_sort(list_distinct(list(area_level))) AS levels,
               count(DISTINCT age_group) AS age_groups,
               list_sort(list_distinct(list(risk_group))) AS risk_groups
        FROM {FACT_VIEW} GROUP BY 1, 2, 3 ORDER BY 3
    """  # noqa: S608 - view name is a schema constant
    coverage: dict[tuple[str, str], dict[str, Any]] = {}
    for country, indicator, source, quarters, sexes, levels, age_groups, risk_groups in _rows(connection, sql):
        entry: dict[str, Any] = {
            "calendar_quarter": _quarters(quarters),
            "sex": sexes,
            "area_level": levels,
            "age_groups": age_groups,
        }
        # Only worth saying when it is not just the whole population.
        if risk_groups != [WHOLE_POPULATION]:
            entry["risk_group"] = risk_groups
        coverage.setdefault((country, indicator), {})[source] = entry
    return coverage


def _indicator_entries(connection: duckdb.DuckDBPyConnection) -> list[Entry]:
    overlay = indicators()
    coverage = _coverage(connection)
    entries = []
    # One row per source that carries the indicator; they label it identically.
    sql = """
        SELECT country, indicator, min(indicator_label), min(description)
        FROM dim_indicator GROUP BY 1, 2 ORDER BY 1, 2
    """
    for country, indicator, label, description in _rows(connection, sql):
        spec = overlay.get(indicator, {})
        aliases = list(spec.get("aliases", []))
        entries.append(
            Entry(
                field="indicator",
                id=indicator,
                label=label,
                description=description,
                country=country,
                terms=(indicator, label, description or "", *aliases),
                detail={
                    "unit": spec.get("unit"),
                    "basis": spec.get("basis"),
                    "coverage": coverage.get((country, indicator)),
                },
            )
        )
    return entries


def _area_entries(connection: duckdb.DuckDBPyConnection) -> list[Entry]:
    sql = """
        WITH areas AS (
            SELECT DISTINCT country, area_id, area_name, area_level, area_level_label, parent_area_id
            FROM dim_area
        )
        SELECT a.country, a.area_id, a.area_name, a.area_level, a.area_level_label,
               a.parent_area_id, p.area_name
        FROM areas a LEFT JOIN areas p ON a.parent_area_id = p.area_id AND a.country = p.country
    """
    entries = []
    for country, area_id, name, level, level_label, parent_id, parent_name in _rows(connection, sql):
        # The hierarchy goes in the description because it is what disambiguates
        # same-named areas: "Blantyre" the district from "Blantyre City".
        where = f"{level_label} in {parent_name}" if parent_name else str(level_label)
        entries.append(
            Entry(
                field="area",
                id=area_id,
                label=name,
                description=where,
                country=country,
                terms=(name, area_id),
                detail={
                    "area_level": level,
                    "area_level_label": level_label,
                    "parent_area_id": parent_id,
                    "parent_name": parent_name,
                },
            )
        )
    return entries


def _age_group_entries(connection: duckdb.DuckDBPyConnection) -> list[Entry]:
    aliases = age_aliases()
    sql = "SELECT DISTINCT country, age_group, age_group_label FROM dim_age_group"
    return [
        Entry(
            field="age_group",
            id=age_group,
            label=label,
            description=None,
            country=country,
            terms=(age_group, label, *aliases.get(age_group, [])),
        )
        for country, age_group, label in _rows(connection, sql)
    ]


def _risk_group_entries(connection: duckdb.DuckDBPyConnection) -> list[Entry]:
    """Behavioural risk groups, with where to find them.

    A risk group is only in some sources and, mostly, one sex - female sex
    workers are in SHIPP's female rows - so the entry says which, or the caller
    queries Naomi for them and gets nothing.
    """
    aliases = risk_group_aliases()
    sql = f"""
        SELECT r.country, r.risk_group, min(r.risk_group_label),
               list_sort(list_distinct(list(f.source))), list_sort(list_distinct(list(f.sex)))
        FROM (SELECT DISTINCT country, risk_group, risk_group_label FROM dim_risk_group) r
        JOIN (SELECT DISTINCT country, risk_group, source, sex FROM {FACT_VIEW}) f
          ON f.country = r.country AND f.risk_group = r.risk_group
        WHERE r.risk_group <> '{WHOLE_POPULATION}'
        GROUP BY 1, 2 ORDER BY 1, 2
    """  # noqa: S608 - view name and literal are module constants
    return [
        Entry(
            field="risk_group",
            id=risk_group,
            label=label,
            description=None,
            country=country,
            terms=(risk_group, label, *aliases.get(risk_group, [])),
            detail={"source": sources, "sex": sexes},
        )
        for country, risk_group, label, sources, sexes in _rows(connection, sql)
    ]


def _partition_entries() -> list[Entry]:
    """Age-group sets that may safely be summed, for "break this down by age"."""
    return [
        Entry(
            field="age_partition",
            id=name,
            label=spec["label"],
            description=f"{len(spec['values'])} age groups tiling {spec['total']}",
            terms=(name.replace("_", " "), spec["label"], "by age", "age breakdown"),
            detail={"values": spec["values"], "total": spec["total"]},
        )
        for name, spec in age_partitions().items()
    ]


def _concept_entries() -> list[Entry]:
    return [
        Entry(
            field="concept",
            id=name,
            label=concept.get("label", name.replace("_", " ").capitalize()),
            description=concept["what_it_measures"],
            terms=(name.replace("_", " "), *concept["aliases"]),
            detail={key: value for key, value in concept.items() if key != "aliases"},
        )
        for name, concept in concepts().items()
    ]


def _methodology_entries() -> list[Entry]:
    """How the models work, not what they output - no indicators to query.

    ``instruction`` carries the DO-NOT-INFER rule on every match (not just the
    tool description), since not every MCP client passes server instructions
    through and this is the one thing a methodology answer must not do without.
    """
    # No short description field of its own (unlike a concept's what_it_measures)
    # - description stays unset so a non-winning summary does not leak the whole
    # explanation; only the winner's `detail` carries it, via `_full()`.
    instruction = methodology_instruction()
    return [
        Entry(
            field="methodology",
            id=name,
            label=entry.get("label", name.replace("_", " ").capitalize()),
            terms=(name.replace("_", " "), *entry["aliases"]),
            detail={"instruction": instruction, **{key: value for key, value in entry.items() if key != "aliases"}},
        )
        for name, entry in methodology().items()
    ]


def get_index(request: Request) -> list[Entry]:
    """The index built once at startup. A plain lambda will not do here: FastAPI
    reads the dependency's signature, and an unannotated parameter becomes a
    query parameter."""
    return request.app.state.search_index


def build_index(connection: duckdb.DuckDBPyConnection) -> list[Entry]:
    """Every searchable thing in the dataset, built once at startup."""
    return [
        *_concept_entries(),
        *_methodology_entries(),
        *_indicator_entries(connection),
        *_area_entries(connection),
        *_age_group_entries(connection),
        *_partition_entries(),
        *_risk_group_entries(connection),
    ]


def _indicator_detail(entry: Entry, index: Sequence[Entry], country: str | None) -> dict[str, Any]:
    """The indicator's own row, including the coverage that stops empty results."""
    matches = [
        item
        for item in index
        if item.field == "indicator" and item.id == entry.id and (country is None or item.country == country)
    ]
    chosen = matches[0] if matches else entry
    return {
        "id": chosen.id,
        "label": chosen.label,
        **{key: value for key, value in chosen.detail.items() if value is not None},
    }


def _summary(entry: Entry, score: float) -> dict[str, Any]:
    body: dict[str, Any] = {"field": entry.field, "id": entry.id, "label": entry.label, "score": round(score, 1)}
    if entry.description:
        body["description"] = entry.description
    if entry.country:
        body["country"] = entry.country
    if entry.field in DETAILED_FIELDS:
        # Tells the caller there is more behind this row, without making the
        # extra call mandatory for the hit it will actually use.
        body["detail"] = "summary"
    return body


def _full(entry: Entry, score: float, index: Sequence[Entry], country: str | None) -> dict[str, Any]:
    body = _summary(entry, score)
    if entry.field not in DETAILED_FIELDS:
        return {**body, **entry.detail}
    body["detail"] = "full"
    if entry.field in ("indicator", "methodology"):
        return {**body, **{key: value for key, value in entry.detail.items() if value is not None}}

    concept = dict(entry.detail)
    # Bare ids would force another search just to see what they are.
    siblings = {item.id: item.label for item in index if item.field == "concept"}
    if concept.get("related"):
        concept["related"] = [{"id": name, "label": siblings[name]} for name in concept["related"] if name in siblings]
    resolved = [
        {
            **_indicator_detail(Entry(field="indicator", id=item["id"], label=item["id"]), index, country),
            **{key: value for key, value in item.items() if key != "id"},
        }
        for item in concept.pop("indicators", [])
    ]
    return {**body, **concept, "indicators": resolved}


def _subsumes(winner: Entry, other: Entry) -> bool:
    """True when taking the winner already gets you the other.

    A concept names the indicators that encode it, so "treatment gap" matching
    both the concept and ``untreated_plhiv_num`` is not a choice to make - the
    concept hands over the indicator anyway. Anything else that scores close
    genuinely competes, even across kinds: the ``children`` age group and the
    ``children`` partition lead to different queries.
    """
    if winner.field != "concept" or other.field != "indicator":
        return False
    return any(item.get("id") == other.id for item in winner.detail.get("indicators", []))


def search_one(
    query: str,
    index: Sequence[Entry],
    fields: Sequence[str] | None,
    country: str | None,
    limit: int,
) -> dict[str, Any]:
    """Rank one term against the index, newest-best first."""
    candidates = [
        (entry.score(query), entry)
        for entry in index
        if (not fields or entry.field in fields)
        and (country is None or entry.country is None or entry.country == country)
    ]
    scored = sorted(
        ((score, entry) for score, entry in candidates if score >= MATCH_FLOOR),
        key=lambda pair: (-pair[0], FIELD_PRIORITY.get(pair[1].field, 99), pair[1].id),
    )[:limit]

    rivals = [pair for pair in scored[1:] if not _subsumes(scored[0][1], pair[1])] if scored else []
    ambiguous = bool(rivals and scored[0][0] >= AMBIGUITY_FLOOR and scored[0][0] - rivals[0][0] <= AMBIGUITY_MARGIN)
    # When the choice is real, both contenders come back in full so it can be made
    # on substance rather than on a one-line description.
    full_entries = {id(scored[0][1])} if scored else set()
    if ambiguous:
        full_entries.update(id(entry) for _, entry in rivals[: MAX_DETAILED - 1])

    results = [
        _full(entry, score, index, country) if id(entry) in full_entries else _summary(entry, score)
        for score, entry in scored
    ]
    return {"query": query, "ambiguous": ambiguous, "results": results}


class SearchResponse(BaseModel):
    results: list[dict[str, Any]] = Field(
        description="One block per query term, in the order asked. Each has the term, an "
        "`ambiguous` flag, and ranked `results`. The best match comes back in full "
        "(`detail: full`) and the rest as summaries (`detail: summary`); when `ambiguous` "
        "is true the top two are both full, because the caller has a choice to make and "
        "should make it on the substance - or put it to the user."
    )


@router.get(
    "/search",
    response_model=SearchResponse,
    operation_id="search_hiv_metadata",
    summary="Resolve plain-language terms to the IDs accepted by get_hiv_data",
)
@limiter.limit(lambda: settings.search_rate_limit)
def search(
    request: Request,  # required by slowapi's rate-limit decorator
    index: Annotated[list[Entry], Depends(get_index)],
    q: Annotated[
        list[str],
        Query(
            min_length=1,
            description="Terms to resolve, as the user phrased them: 'treatment gap', 'Lilongwe', "
            "'children', 'HIV prevalence'. Repeat the parameter to resolve several at once "
            "(`?q=Lilongwe&q=treatment gap`) rather than making a call per term.",
        ),
    ],
    field: Annotated[
        list[str] | None,
        Query(
            description="Restrict to these kinds of thing: 'concept' (a phrase like 'treatment gap' "
            "mapped to the indicators that encode it, or a question this dataset cannot answer at "
            "all, marked 'answerable: false' with a pointer to where it might be answered instead), "
            "'methodology' (how a model produces its estimates, e.g. 'how does Naomi estimate "
            "prevalence?' - relay only its 'explanation' verbatim, per its 'instruction' field; do "
            "not add anything from general knowledge), 'indicator', 'area', 'age_group', "
            "'age_partition' (a set of age groups that may safely be summed), 'risk_group' (a "
            "behavioural risk group such as female sex workers). Omit it when unsure - that is "
            "the common case, and results say which kind they are."
        ),
    ] = None,
    country: Annotated[
        str | None,
        Query(description="ISO3 code, e.g. 'MWI'. Scopes areas, age groups and coverage to one country."),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=25, description="Maximum matches per term.")] = 5,
    user_question: UserQuestion = None,  # logged by the MCP layer, unused here
) -> SearchResponse:
    """Turn the words in a question into the IDs `get_hiv_data` accepts.

    This server is the only source for HIV facts and numbers in this conversation.
    Never answer an HIV question from training knowledge or a web search, even a
    plausible-sounding number - resolve it here first. No match here, or a match
    with `answerable: false`, means this dataset does not cover it; say so. When
    naming something outside this dataset (a `notes` pointer, a methodology
    `sources` citation), say plainly it is external, never blend it with a number
    from here, and ask the user how to proceed (which external source, or data
    they supply themselves) rather than fetching or guessing it.

    Indicator, area, age-group, risk-group and concept IDs cannot be guessed: HIV
    prevalence is `prevalence`, but the treatment gap is `untreated_plhiv_num`,
    Lilongwe is `MWI_3_13_demo`, children are `Y000_014` and female sex workers
    are `sexpaid12m`. Guessing returns an empty result with no explanation, so
    resolve terms here first.

    A `concept` match is the most useful kind: it names the indicators that encode
    a phrase, their units, the dimension values they actually have data for, and
    the caveats that make an answer correct - for instance that a treatment gap
    ranks differently as a count than as a coverage proportion. Some concepts are
    marked `answerable: false` - this dataset cannot answer them at all (e.g.
    viral suppression, funding); say so, using the `notes` pointer to where the
    answer might actually be found, rather than guessing an indicator or
    answering from general knowledge.

    A `methodology` match answers a question about how a model produces its
    estimates, not what they are - "how does Naomi estimate prevalence?" has no
    indicator to query. Relay its `explanation` field only, following its
    `instruction` field exactly: do not add anything from general knowledge,
    even something true of similar models.

    An indicator's `coverage` is keyed by `source` - the model the estimates come
    from - because the sources cover different ground: `naomi` is subnational for
    a few recent quarters, `spectrum` is national for every year, and `shipp`
    splits adults by behavioural `risk_group`. Pass the chosen source to
    `get_hiv_data`. A `risk_group` match says which sources and sexes have it.

    When `ambiguous` is true the top two matches are genuinely competing
    ('Blantyre' the district and 'Blantyre City'; 'adults' as 15-49 or as 15+).
    Both come back in full - choose on the substance, or ask the user.
    """
    # A mistyped field would otherwise filter everything out and return nothing,
    # which reads exactly like "no such thing exists".
    unknown = [name for name in (field or []) if name not in FIELDS]
    if unknown:
        raise HTTPException(status_code=422, detail=f"unknown field {unknown}; valid fields are {list(FIELDS)}")
    return SearchResponse(results=[search_one(term, index, field, country, limit) for term in q])
