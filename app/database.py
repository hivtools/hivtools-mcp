"""DuckDB access to the Naomi Parquet dataset.

The dataset written by ``data-prep/extract_indicators.py`` is a fact table plus
four dimension tables, each Hive-partitioned by country, and a JSON manifest of
provenance. Each becomes a view on one shared connection; the manifest is read
once into application state.

Concurrency model: one in-process DuckDB connection is created at startup and
shared. It is never written to - the durable data is Parquet, which any number of
readers can open - so there is no database file to lock and no writer to contend
with. Each request gets its own short-lived ``cursor()`` off the shared
connection (DuckDB's unit of concurrent execution); the ``/data`` endpoint is a
sync ``def``, so Starlette runs it in its worker threadpool and DuckDB releases
the GIL for the duration of the scan, giving genuine parallel reads.
"""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Any

import duckdb
from fastapi import FastAPI, Request

from app.schema import EMPTY_DATASET_SQL, EMPTY_DIM_SQL
from app.settings import settings

VIEW_NAME = "indicators"
FACTS_SUBDIR = "facts"
MANIFEST_FILE = "manifest.json"


def _create_view(connection: duckdb.DuckDBPyConnection, view: str, subdir: str, empty_sql: str) -> None:
    """Expose one Parquet table as a view, falling back to a typed empty one."""
    directory = settings.naomi_data_dir / subdir
    if directory.is_dir() and any(directory.glob("**/*.parquet")):
        # The glob is trusted config and is passed as a Python argument to the
        # relation API, never interpolated into a SQL string.
        relation = connection.read_parquet(f"{directory}/**/*.parquet", hive_partitioning=True)
    else:
        relation = connection.sql(empty_sql)
    relation.create_view(view)


def create_connection() -> duckdb.DuckDBPyConnection:
    """Open the shared connection and expose the fact and dimension tables."""
    connection = duckdb.connect(":memory:")
    if settings.duckdb_threads is not None:
        connection.execute(f"SET threads TO {int(settings.duckdb_threads)}")

    _create_view(connection, VIEW_NAME, FACTS_SUBDIR, EMPTY_DATASET_SQL)
    for view, empty_sql in EMPTY_DIM_SQL.items():
        _create_view(connection, view, view, empty_sql)
    return connection


def load_manifest(data_dir: Path | None = None) -> dict[str, Any]:
    """Per-country provenance written by data-prep, keyed by ISO3 code.

    Absent for a dataset built before manifests, or none at all; callers treat a
    missing entry as "provenance unknown" rather than an error.
    """
    path = (data_dir or settings.naomi_data_dir) / MANIFEST_FILE
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    return document.get("countries", {})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.db = create_connection()
    app.state.manifest = load_manifest()
    try:
        yield
    finally:
        app.state.db.close()


def get_cursor(request: Request) -> Iterator[duckdb.DuckDBPyConnection]:
    """Per-request DuckDB cursor off the shared connection."""
    with closing(request.app.state.db.cursor()) as cursor:
        yield cursor


def get_manifest(request: Request) -> dict[str, Any]:
    return request.app.state.manifest
