"""The ``/data`` endpoint: filtered reads of the indicators dataset.

Response shape: dimensions that are identical across every matching row are
hoisted into a ``meta`` block and dropped from the rows, because the common query
here pins five of the six dimensions and varies one ("this indicator, this
quarter, every district"). Repeating the pinned values on every row is most of
the payload and none of the information. ``meta`` is also where a value becomes
interpretable - the unit that says 0.108 is 10.8% rather than 0.108%, and the
provenance that says these are demonstration figures.

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
        "receive care there). `source` names the model run the figures come from."
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
        "whichever were requested via `columns` (all six by default)."
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

    # The country's display name and its provenance both come from the manifest;
    # neither is a column on the fact table.
    country = constants.get("country")
    entry = manifest.get(country) if country is not None else None
    if entry is not None:
        meta["source"] = entry["source"]
        label = entry.get("country_label")
        if label:
            meta["country"] = {"id": country, "label": label}
    else:
        sources = sorted({item["source"] for item in manifest.values() if item.get("source")})
        if len(sources) == 1:
            meta["source"] = sources[0]
        elif sources:
            meta["source"] = sources
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
    summary="Query HIV epidemic indicator estimates from the Naomi model",
)
@limiter.limit(settings.data_rate_limit)
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
            "'MWI_1_1_demo' for a level-1 subdivision. Matches the area_id used in Naomi model output; "
            "there is no fixed list, so discover them by querying a country with no area_id filter."
        ),
    ] = None,
    sex: Annotated[
        list[str] | None,
        Query(description="Sex the estimate applies to: 'both', 'male', or 'female'."),
    ] = None,
    age_group: Annotated[
        list[str] | None,
        Query(description="Age band as 'Y<low>_<high>', e.g. 'Y015_049' for ages 15-49 or 'Y000_999' for all ages."),
    ] = None,
    calendar_quarter: Annotated[
        list[str] | None,
        Query(description="Estimate quarter as 'CY<year>Q<quarter>', e.g. 'CY2024Q3' for Q3 2024."),
    ] = None,
    indicator: Annotated[
        list[str] | None,
        Query(
            description="Which modelled quantity to return, e.g. 'prevalence' (HIV prevalence), "
            "'incidence' (new infections), 'art_coverage' (proportion on ART), 'art_current' "
            "(number currently on ART). The full set varies by model run and isn't enumerable here - "
            "call with no `indicator` filter and inspect the `indicator` column of the response to "
            "see what's available."
        ),
    ] = None,
    age_partition: Annotated[
        str | None,
        Query(
            description="Name of an age-group set that tiles a population without overlapping, e.g. "
            "'five_year_bands'. Expands to that set's age groups, so a breakdown by age can be "
            "requested without enumerating codes or risking an overlapping selection. Find the "
            "available names with search (field='age_partition'). Cannot be combined with `age_group`."
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
    """Filtered rows from the Naomi HIV model's indicator estimates.

    Each row is one modelled estimate for a unique combination of the dimensions
    (country, area_level, area_id, sex, age_group, calendar_quarter, indicator);
    `columns` picks which summary-statistic measures (mean, se, median, mode,
    lower, upper) come back alongside them.

    Dimensions that are the same for every matching row are returned once in
    `meta` rather than repeated on each row, so read `meta` first: it carries the
    labels, the `unit` needed to interpret a value, and the `source` of the
    estimates. Rows carry only what varies, with labels alongside codes
    (`area_id` and `area_name`).

    Filters combine with AND across different parameters; repeating a parameter
    or comma-separating its value is OR within that parameter, e.g.
    `?indicator=prevalence&indicator=incidence` and `?indicator=prevalence,incidence`
    are equivalent. Omitting a filter matches every value for that dimension.
    """
    figs = sig_figs if sig_figs is not None else settings.response_sig_figs
    filters: dict[str, Sequence[Any] | None] = {
        "country": _split(country),
        "area_level": _levels(area_level),
        "area_id": _split(area_id),
        "sex": _split(sex),
        "age_group": _age_groups(age_group, age_partition),
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
