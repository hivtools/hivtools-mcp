"""The ``/data`` endpoint: filtered reads of the indicators dataset.

Response shape: dimensions that are identical across every matching row are
hoisted into a ``meta`` block and dropped from the rows, because the common query
here pins all but one dimension and varies one ("this indicator, this quarter,
every district"). Repeating the pinned values on every row is most of the payload
and none of the information. ``meta`` is also where a value becomes
interpretable - the unit that says 0.108 is 10.8% rather than 0.108%, and the
provenance that says how the figures must be described.

A dimension is only hoisted when it is *provably* constant across the whole
result: either the caller pinned it to a single value, or this page is the entire
result set and every row agrees. Hoisting a value that merely happens to be
constant on page one would silently mislabel page two.

Design: a single flat fact table with a fixed set of categorical dimensions and
numeric measures maps cleanly onto REST query parameters, so that is what this
is. Every dimension is an ``IN`` filter; ``columns`` picks which measures come
back (dimension columns are always returned). List parameters accept either a
repeated key (``?indicator=a&indicator=b``) or a comma-separated value
(``?indicator=a,b``). GraphQL would buy little here - there is no graph to
traverse - and would add a schema/resolver layer and query-cost controls.

Injection safety: column names only ever come from ``app.schema`` allowlists;
every value supplied by the caller is passed to DuckDB as a bound ``?``
parameter, never string-formatted into the SQL.
"""

import math
from collections.abc import Sequence
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app.database import VIEW_NAME, get_cursor, get_manifest
from app.diagnostics import diagnose
from app.knowledge.loader import age_partitions
from app.knowledge.loader import indicators as indicator_knowledge
from app.observability import UserQuestion
from app.query import count_rows, where_clause
from app.ratelimit import limiter
from app.schema import (
    DIMENSION_LABELS,
    DIMENSION_LOOKUPS,
    DIMENSIONS,
    LABEL_COLUMNS,
    MEASURES,
    ORDER_BY,
)
from app.search import Entry, get_index
from app.settings import settings

router = APIRouter()

MAX_SIG_FIGS = 15  # a float64 carries ~15-17 significant decimal digits


class DataResponse(BaseModel):
    meta: dict[str, Any] = Field(
        description="Dimensions identical across every matching row, hoisted out of the rows "
        "so they are stated once. Labelled dimensions appear as `{id, label}`. When the "
        "indicator is one of them it also carries `unit` (`proportion` is a fraction 0-1, so "
        "0.108 means 10.8%; `count` is people; `rate_per_person_year` is per person per year) "
        "and `basis` (`residents` = people who live in the area, `attending` = people who "
        "receive care there). `source` is the model the figures come from, and its `label` "
        "says how they must be described when reported. When rows come from several sources, "
        "`sources` gives that description for each."
    )
    total: int = Field(
        description="Total rows matching the filters, ignoring `limit`/`offset`."
        " Lets a caller page and show 'x of total' without pulling everything."
    )
    diagnostic: str | None = Field(
        default=None,
        description="Present only when nothing matched, saying why. Either a value is not in the "
        "data at all (with the nearest real ones), or every value is valid but the combination "
        "is empty - in which case it names the filter responsible and the values that would have "
        "worked. An empty result is otherwise indistinguishable from a true negative, so read "
        "this rather than concluding there is no data.",
    )
    data: list[dict[str, Any]] = Field(
        description="The current page of matching rows, carrying only the dimensions that "
        "vary across the result - everything else is in `meta`. A varying dimension with a "
        "label appears as both, e.g. `area_id` alongside `area_name`. Measure columns are "
        "whichever were requested via `columns` (all six by default); Spectrum and SHIPP "
        "estimates only have `mean`, so their other measures are null."
    )


def _split(values: list[str] | None) -> list[str] | None:
    """Flatten repeated and/or comma-separated query values into one list."""
    if not values:
        return None
    flat = [item.strip() for value in values for item in value.split(",") if item.strip()]
    return flat or None


def _levels(values: list[str] | None) -> list[int] | None:
    parsed = _split(values)
    if parsed is None:
        return None
    try:
        return [int(value) for value in parsed]
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"area_level values must be integers, got {parsed}",
        ) from exc


def _round_sig(value: float, sig_figs: int) -> float:
    """Round to a number of significant figures (handles proportions and counts alike)."""
    if value == 0 or not math.isfinite(value):
        return value
    return round(value, sig_figs - 1 - math.floor(math.log10(abs(value))))


def _age_groups(age_group: list[str] | None, age_partition: str | None) -> list[str] | None:
    """The age groups to filter on, expanding a partition name if one was given.

    Expanding server-side rather than making the caller enumerate the codes is the
    point of naming partitions at all: a caller that builds the list itself can
    build one that overlaps, and nothing about the result would say so.
    """
    explicit = _split(age_group)
    if age_partition is None:
        return explicit
    if explicit:
        raise HTTPException(
            status_code=422,
            detail="pass either age_group or age_partition, not both",
        )
    partition = age_partitions().get(age_partition)
    if partition is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown age_partition {age_partition!r}; valid partitions are {sorted(age_partitions())}",
        )
    return list(partition["values"])


def _measures(values: list[str] | None) -> list[str]:
    requested = _split(values)
    if requested is None:
        return list(MEASURES)
    unknown = [value for value in requested if value not in MEASURES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown columns {unknown}; valid measures are {list(MEASURES)}",
        )
    return list(dict.fromkeys(requested))


def build_data_query(
    filters: dict[str, Sequence[Any] | None],
    measures: Sequence[str],
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    selected = ", ".join(f'"{col}"' for col in (*DIMENSIONS, *LABEL_COLUMNS, *measures))
    # Ordering by the dimension tables' sort keys, not the codes: a code sort puts
    # 'Y000_999' (all ages) in the middle of the five-year bands.
    order = ", ".join(f'"{col}"' for col in ORDER_BY)
    where_sql, params = where_clause(filters)
    # S608: identifiers are schema constants, values are all in `params`.
    sql = f"SELECT {selected} FROM {VIEW_NAME}{where_sql} ORDER BY {order} LIMIT ? OFFSET ?"  # noqa: S608
    return sql, [*params, limit, offset]


def _lookup_label(cursor: duckdb.DuckDBPyConnection, dimension: str, value: Any) -> str | None:
    """Label for a dimension value from its dimension table.

    Only needed when no row came back to read the denormalised label off - which
    is exactly when the caller most needs to be told what they asked for.
    """
    lookup = DIMENSION_LOOKUPS.get(dimension)
    if lookup is None:
        return None
    view, key, label = lookup
    # S608: view and column names are schema constants; the value is bound.
    row = cursor.execute(f'SELECT "{label}" FROM {view} WHERE "{key}" = ? LIMIT 1', [value]).fetchone()  # noqa: S608
    return row[0] if row else None


def constant_dimensions(
    filters: dict[str, Sequence[Any] | None],
    rows: Sequence[dict[str, Any]],
    total: int,
) -> dict[str, Any]:
    """Dimensions provably identical across the whole result, not just this page.

    Two ways to know: the caller pinned the dimension to one value, or this page
    is the entire result set and every row agrees. Anything else - a value that
    merely happens to be constant on page one - is not safe to hoist.
    """
    constants: dict[str, Any] = {}
    page_is_whole_result = len(rows) == total
    for dimension in DIMENSIONS:
        values = filters.get(dimension)
        if values and len(set(values)) == 1:
            constants[dimension] = values[0]
        elif rows and page_is_whole_result:
            distinct = {row[dimension] for row in rows}
            if len(distinct) == 1:
                constants[dimension] = distinct.pop()
    return constants


def source_descriptions(manifest: dict[str, Any], country: str | None, source: str | None) -> dict[str, dict[str, str]]:
    """How each source in scope must be described, per country, from the manifest."""
    countries = [country] if country is not None else sorted(manifest)
    found: dict[str, dict[str, str]] = {}
    for name in countries:
        for key, entry in manifest.get(name, {}).get("sources", {}).items():
            if (source is None or key == source) and entry.get("description"):
                found.setdefault(name, {})[key] = entry["description"]
    return found


def build_meta(
    cursor: duckdb.DuckDBPyConnection,
    constants: dict[str, Any],
    rows: Sequence[dict[str, Any]],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """The hoisted dimensions, plus what makes their values interpretable."""
    meta: dict[str, Any] = {}
    for dimension, value in constants.items():
        label_column = DIMENSION_LABELS.get(dimension)
        if label_column is None:
            meta[dimension] = value
            continue
        label = rows[0][label_column] if rows else _lookup_label(cursor, dimension, value)
        meta[dimension] = {"id": value, "label": label} if label is not None else {"id": value}

    # Units and basis are not in the model output; they come from the knowledge
    # layer, and without them a proportion is indistinguishable from a percentage.
    indicator = constants.get("indicator")
    if indicator is not None:
        spec = indicator_knowledge().get(indicator)
        if spec is not None:
            meta["indicator"] = {**meta["indicator"], "unit": spec["unit"], "basis": spec["basis"]}

    # The country's display name and how its figures must be described both come
    # from the manifest; neither is a column on the fact table.
    country = constants.get("country")
    label = manifest.get(country, {}).get("country_label") if country is not None else None
    if label:
        meta["country"] = {"id": country, "label": label}

    source = constants.get("source")
    described = source_descriptions(manifest, country, source)
    if source is not None:
        meta["source"] = {"id": source}
        if country is not None and source in described.get(country, {}):
            meta["source"]["label"] = described[country][source]
            return meta
    if described:
        meta["sources"] = described[country] if country is not None else described
    return meta


def strip_constants(row: dict[str, Any], constants: dict[str, Any]) -> dict[str, Any]:
    """Drop hoisted dimensions, and the label columns that travel with them."""
    hoisted = set(constants) | {DIMENSION_LABELS[name] for name in constants if name in DIMENSION_LABELS}
    return {name: value for name, value in row.items() if name not in hoisted}


@router.get(
    "/data",
    response_model=DataResponse,
    response_model_exclude_none=True,
    operation_id="get_hiv_data",
    summary="Query HIV estimates from the Naomi, Spectrum and SHIPP models",
)
@limiter.limit(lambda: settings.data_rate_limit)
def get_data(
    request: Request,  # required by slowapi's rate-limit decorator
    response: Response,
    cursor: Annotated[duckdb.DuckDBPyConnection, Depends(get_cursor)],
    manifest: Annotated[dict[str, Any], Depends(get_manifest)],
    index: Annotated[list[Entry], Depends(get_index)],
    country: Annotated[
        list[str] | None,
        Query(description="ISO3 country code, e.g. 'MWI', 'ZWE'. The partition key, so filtering by it is cheap."),
    ] = None,
    source: Annotated[
        list[str] | None,
        Query(
            description="Model the estimates come from: 'naomi' (subnational, a few recent quarters, with "
            "uncertainty intervals), 'spectrum' (national only, one value per year since 1970 - later years "
            "are projections) or 'shipp' (adults 15-49 split by behavioural `risk_group`). An indicator from "
            "two sources is the same quantity estimated by two models: pick one, never add them. Which "
            "sources an indicator has is in its coverage from `search_hiv_metadata`. ALWAYS tell "
            "the user what source the value is from."
        ),
    ] = None,
    area_level: Annotated[
        list[str] | None,
        Query(
            description="Spatial aggregation level as an integer: 0 is national, and each level up "
            "is a finer subdivision (typically up to 4). Higher numbers mean smaller, more granular areas."
        ),
    ] = None,
    area_id: Annotated[
        list[str] | None,
        Query(
            description="Specific area code within `area_level`, e.g. 'MWI' for the national area or "
            "'MWI_1_1_demo' for a level-1 subdivision. Not guessable from an area's name: resolve "
            "names with `search_hiv_metadata`, which also tells same-named areas apart."
        ),
    ] = None,
    sex: Annotated[
        list[str] | None,
        Query(description="Sex the estimate applies to: 'both', 'male', or 'female'."),
    ] = None,
    age_group: Annotated[
        list[str] | None,
        Query(
            description="Age band as 'Y<low>_<high>', e.g. 'Y015_049' for ages 15-49 or 'Y000_999' for all "
            "ages. Resolve phrases like 'children' or 'adults' with `search_hiv_metadata`."
        ),
    ] = None,
    risk_group: Annotated[
        list[str] | None,
        Query(
            description="Behavioural risk group, e.g. 'sexpaid12m' (female sex workers), 'msm' or 'pwid'. "
            "'all' is the whole population and the only value Naomi and Spectrum have; the rest come from "
            "source 'shipp'. Within one sex the groups do not overlap and add up to the whole population. "
            "Resolve names like 'sex workers' with `search_hiv_metadata`, which says which sexes have each."
        ),
    ] = None,
    calendar_quarter: Annotated[
        list[str] | None,
        Query(
            description="Estimate quarter as 'CY<year>Q<quarter>', e.g. 'CY2024Q3' for Q3 2024. Not every "
            "indicator or source covers every quarter - Spectrum has one quarter per year - so check the "
            "indicator's coverage from `search_hiv_metadata`."
        ),
    ] = None,
    indicator: Annotated[
        list[str] | None,
        Query(
            description="Which modelled quantity to return, e.g. 'prevalence' or 'art_coverage'. Most IDs "
            "are not guessable from a plain-language name (the treatment gap is 'untreated_plhiv_num'), "
            "so resolve them with `search_hiv_metadata`, which also returns each indicator's unit and "
            "the quarters it covers."
        ),
    ] = None,
    age_partition: Annotated[
        str | None,
        Query(
            description="Name of an age-group set that tiles a population without overlapping, e.g. "
            "'five_year_bands'. Expands to that set's age groups, so a breakdown by age can be "
            "requested without enumerating codes or risking an overlapping selection. Find the "
            "available names with `search_hiv_metadata` (field='age_partition'). Cannot be combined with `age_group`."
        ),
    ] = None,
    columns: Annotated[
        list[str] | None,
        Query(
            description=f"Measure columns to return (repeat or comma-separate). One or more of {list(MEASURES)}; defaults to all."
        ),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=settings.max_rows, description="Max rows to return in this page.")
    ] = settings.default_rows,
    offset: Annotated[
        int,
        Query(
            ge=0,
            description="Rows to skip before this page starts. Results are ordered by the "
            "dimension columns, so paging with `limit`/`offset` is deterministic.",
        ),
    ] = 0,
    sig_figs: Annotated[
        int | None,
        Query(ge=1, le=MAX_SIG_FIGS, description="Significant figures for measure values; default from config."),
    ] = None,
    user_question: UserQuestion = None,  # logged by the MCP layer, unused here
) -> DataResponse:
    """Filtered rows of modelled HIV estimates.

    Call `search_hiv_metadata` first to resolve every indicator, area, age group,
    age partition and risk group in the user's question to an ID. IDs are not
    guessable (the treatment gap is `untreated_plhiv_num`, Lilongwe is
    `MWI_3_13_demo`), a wrong one returns no rows, and search also says which
    sources and quarters each indicator covers.

    Each row is one modelled estimate for a unique combination of the dimensions
    (country, source, area_level, area_id, sex, age_group, risk_group,
    calendar_quarter, indicator); `columns` picks which summary-statistic
    measures (mean, se, median, mode, lower, upper) come back alongside them.

    `source` is the model behind a row. `naomi` is subnational, for a few recent
    quarters, with uncertainty intervals. `spectrum` is national only, one value
    per year from 1970 - use it for long-term trends, and say that future years
    are projections. `shipp` splits adults 15-49 by behavioural `risk_group`
    (female sex workers, MSM, PWID and others) for one year. Spectrum and SHIPP
    give point estimates only. An indicator from two sources is two estimates of
    the same quantity: pass `source` to pick one, and never add rows from
    different sources together. Not every country has every source.

    Dimensions that are the same for every matching row are returned once in
    `meta` rather than repeated on each row, so read `meta` first: it carries the
    labels and the `unit` needed to interpret a value. Its `source` label (or
    `sources`) says how the figures must be described - use that wording when
    reporting them, and call demonstration data synthetic. Rows carry only what
    varies, with labels alongside codes (`area_id` and `area_name`).

    Filters combine with AND across different parameters; repeating a parameter
    or comma-separating its value is OR within that parameter, e.g.
    `?indicator=prevalence&indicator=incidence` and `?indicator=prevalence,incidence`
    are equivalent. Omitting a filter matches every value for that dimension.
    """
    figs = sig_figs if sig_figs is not None else settings.response_sig_figs
    filters: dict[str, Sequence[Any] | None] = {
        "country": _split(country),
        "source": _split(source),
        "area_level": _levels(area_level),
        "area_id": _split(area_id),
        "sex": _split(sex),
        "age_group": _age_groups(age_group, age_partition),
        "risk_group": _split(risk_group),
        "calendar_quarter": _split(calendar_quarter),
        "indicator": _split(indicator),
    }
    measures = _measures(columns)

    sql, params = build_data_query(filters, measures, limit, offset)
    cursor.execute(sql, params)
    names = [description[0] for description in cursor.description]
    rows = [
        {
            name: _round_sig(value, figs) if isinstance(value, float) else value
            for name, value in zip(names, row, strict=True)
        }
        for row in cursor.fetchall()
    ]

    total = count_rows(cursor, filters)

    constants = constant_dimensions(filters, rows, total)
    meta = build_meta(cursor, constants, rows, manifest)
    if age_partition is not None:
        partition = age_partitions()[age_partition]
        meta["age_partition"] = {
            "id": age_partition,
            "label": partition["label"],
            "tiles": partition["total"],
        }

    response.headers["Cache-Control"] = f"public, max-age={settings.cache_max_age}"
    return DataResponse(
        meta=meta,
        total=total,
        # Only paid for on the empty path, which is where the caller is stuck.
        diagnostic=diagnose(cursor, filters, index) if total == 0 else None,
        data=[strip_constants(row, constants) for row in rows],
    )
