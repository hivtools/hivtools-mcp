"""Health endpoints for the Azure Container Apps probes.

``/health`` is liveness - the process is up, no work done. ``/health/ready`` is
readiness - the DuckDB connection answers a trivial query, so the dataset (or the
empty-dataset fallback) is loaded and the app can serve ``/data``.
"""

from typing import Annotated

import duckdb
from fastapi import APIRouter, Depends, HTTPException

from app.database import get_cursor

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
def health_ready(cursor: Annotated[duckdb.DuckDBPyConnection, Depends(get_cursor)]) -> dict[str, str]:
    try:
        cursor.execute("SELECT 1").fetchone()
    except Exception as exc:  # any failure here means "not ready"
        raise HTTPException(status_code=503, detail="database not ready") from exc
    return {"status": "ready"}
