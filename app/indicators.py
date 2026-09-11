"""The ``/data`` endpoint: filtered reads of the indicators dataset.

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

from app.database import VIEW_NAME, get_cursor
from app.ratelimit import limiter
from app.schema import DIMENSIONS, MEASURES
from app.settings import settings

router = APIRouter()

MAX_SIG_FIGS = 15  # a float64 carries ~15-17 significant decimal digits


class DataResponse(BaseModel):
    total: int = Field(
        description="Total rows matching the filters, ignoring `limit`/`offset`."
        " Lets a caller page and show 'x of total' without pulling everything."
    )
    data: list[dict[str, Any]] = Field(
        description="The current page of matching rows. Every row includes all seven "
        "dimension columns (country, area_level, area_id, sex, age_group, "
        "calendar_quarter, indicator) plus whichever measure columns were "
        "requested via `columns` (all six by default)."
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


def _where_clause(filters: dict[str, Sequence[Any] | None]) -> tuple[str, list[Any]]:
    """Build the shared ``WHERE`` for the data and count queries.

    ``filters`` keys are trusted (schema column names); all values are bound.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for column, values in filters.items():
        if not values:
            continue
        placeholders = ", ".join("?" for _ in values)
        conditions.append(f'"{column}" IN ({placeholders})')
        params.extend(values)
    clause = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return clause, params


def build_data_query(
    filters: dict[str, Sequence[Any] | None],
    measures: Sequence[str],
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    selected = ", ".join(f'"{col}"' for col in (*DIMENSIONS, *measures))
    order = ", ".join(f'"{col}"' for col in DIMENSIONS)
    where_sql, params = _where_clause(filters)
    # S608: identifiers are schema constants, values are all in `params`.
    sql = f"SELECT {selected} FROM {VIEW_NAME}{where_sql} ORDER BY {order} LIMIT ? OFFSET ?"  # noqa: S608
    return sql, [*params, limit, offset]


def build_count_query(filters: dict[str, Sequence[Any] | None]) -> tuple[str, list[Any]]:
    where_sql, params = _where_clause(filters)
    return f"SELECT count(*) FROM {VIEW_NAME}{where_sql}", params  # noqa: S608


@router.get(
    "/data",
    response_model=DataResponse,
    operation_id="get_hiv_data",
    summary="Query HIV epidemic indicator estimates from the Naomi model",
)
@limiter.limit(settings.data_rate_limit)
def get_data(
    request: Request,  # required by slowapi's rate-limit decorator
    response: Response,
    cursor: Annotated[duckdb.DuckDBPyConnection, Depends(get_cursor)],
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
) -> DataResponse:
    """Filtered rows from the Naomi HIV model's indicator estimates.

    Each row is one modelled estimate for a unique combination of the dimensions
    (country, area_level, area_id, sex, age_group, calendar_quarter, indicator);
    `columns` picks which summary-statistic measures (mean, se, median, mode,
    lower, upper) come back alongside them.

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
        "age_group": _split(age_group),
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

    count_sql, count_params = build_count_query(filters)
    count_row = cursor.execute(count_sql, count_params).fetchone()
    total = int(count_row[0]) if count_row else 0

    response.headers["Cache-Control"] = f"public, max-age={settings.cache_max_age}"
    return DataResponse(total=total, data=rows)
