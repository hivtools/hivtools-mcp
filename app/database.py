"""DuckDB access to the indicators Parquet dataset.

Concurrency model: one in-process DuckDB connection is created at startup and
shared. It is never written to - the durable data is Parquet, which any number of
readers can open - so there is no database file to lock and no writer to contend
with. Each request gets its own short-lived ``cursor()`` off the shared
connection (DuckDB's unit of concurrent execution); the ``/data`` endpoint is a
sync ``def``, so Starlette runs it in its worker threadpool and DuckDB releases
the GIL for the duration of the scan, giving genuine parallel reads.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing

import duckdb
from fastapi import FastAPI, Request

from app.schema import EMPTY_DATASET_SQL
from app.settings import settings

VIEW_NAME = "indicators"


def create_connection() -> duckdb.DuckDBPyConnection:
    """Open the shared connection and expose the dataset as the ``indicators`` view."""
    connection = duckdb.connect(":memory:")
    if settings.duckdb_threads is not None:
        connection.execute(f"SET threads TO {int(settings.duckdb_threads)}")

    data_dir = settings.naomi_data_dir
    if any(data_dir.glob("**/*.parquet")):
        # The glob is trusted config and is passed as a Python argument to the
        # relation API, never interpolated into a SQL string.
        relation = connection.read_parquet(f"{data_dir}/**/*.parquet", hive_partitioning=True)
    else:
        relation = connection.sql(EMPTY_DATASET_SQL)
    relation.create_view(VIEW_NAME)
    return connection


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.db = create_connection()
    try:
        yield
    finally:
        app.state.db.close()


def get_cursor(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """Per-request DuckDB cursor off the shared connection."""
    with closing(request.app.state.db.cursor()) as cursor:
        yield cursor
