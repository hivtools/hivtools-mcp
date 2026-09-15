"""Filter-to-SQL helpers shared by the data endpoint and its diagnostics.

Injection safety: column names only ever come from ``app.schema`` allowlists;
every value supplied by the caller is passed to DuckDB as a bound ``?``
parameter, never string-formatted into the SQL.
"""

from collections.abc import Sequence
from typing import Any

import duckdb

from app.schema import FACT_VIEW

Filters = dict[str, Sequence[Any] | None]


def where_clause(filters: Filters) -> tuple[str, list[Any]]:
    """The shared ``WHERE`` for data, count and diagnostic queries.

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


def count_rows(cursor: duckdb.DuckDBPyConnection, filters: Filters) -> int:
    """How many rows match, ignoring pagination."""
    clause, params = where_clause(filters)
    row = cursor.execute(f"SELECT count(*) FROM {FACT_VIEW}{clause}", params).fetchone()  # noqa: S608
    return int(row[0]) if row else 0


def distinct_values(cursor: duckdb.DuckDBPyConnection, column: str, filters: Filters) -> list[Any]:
    """The values of ``column`` present under ``filters``, in order.

    Used to answer "what could I have asked for instead", so the caller gets the
    available values rather than being told only that theirs were wrong.
    """
    clause, params = where_clause(filters)
    sql = f'SELECT DISTINCT "{column}" FROM {FACT_VIEW}{clause} ORDER BY 1'  # noqa: S608
    return [row[0] for row in cursor.execute(sql, params).fetchall()]
