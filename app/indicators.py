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

from collections.abc import Sequence
from typing import Annotated, Any

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.database import VIEW_NAME, get_cursor
from app.schema import DIMENSIONS, MEASURES

router = APIRouter()

MAX_ROWS = 50_000
DEFAULT_ROWS = 1_000


class DataResponse(BaseModel):
    # Rows matching the filters, ignoring limit/offset - lets a caller page and
    # show "x of total" without pulling everything.
    total: int
    data: list[dict[str, Any]]


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


@router.get("/data", response_model=DataResponse)
def get_data(
    cursor: Annotated[duckdb.DuckDBPyConnection, Depends(get_cursor)],
    country: Annotated[list[str] | None, Query()] = None,
    area_level: Annotated[list[str] | None, Query()] = None,
    area_id: Annotated[list[str] | None, Query()] = None,
    sex: Annotated[list[str] | None, Query()] = None,
    age_group: Annotated[list[str] | None, Query()] = None,
    calendar_quarter: Annotated[list[str] | None, Query()] = None,
    indicator: Annotated[list[str] | None, Query()] = None,
    columns: Annotated[
        list[str] | None,
        Query(
            description=f"Measure columns to return (repeat or comma-separate). One or more of {list(MEASURES)}; defaults to all."
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = DEFAULT_ROWS,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DataResponse:
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
    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    count_sql, count_params = build_count_query(filters)
    count_row = cursor.execute(count_sql, count_params).fetchone()
    total = int(count_row[0]) if count_row else 0

    return DataResponse(total=total, data=rows)
