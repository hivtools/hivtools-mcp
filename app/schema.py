"""The fixed column set of the indicators dataset.

Centralised so the query builder and the empty-dataset fallback view cannot drift
apart, and so every column name that reaches SQL comes from here rather than from
a request.
"""

from typing import Literal, get_args

# Categorical columns: queryable as filters and always returned.
DIMENSIONS: tuple[str, ...] = (
    "country",
    "area_level",
    "area_id",
    "sex",
    "age_group",
    "calendar_quarter",
    "indicator",
)

# Estimate columns the caller may ask for. ``Measure`` is the single source of
# truth (a static type usable in the endpoint signature); ``MEASURES`` is just
# its members at runtime.
Measure = Literal["mean", "se", "median", "mode", "lower", "upper"]
MEASURES: tuple[Measure, ...] = get_args(Measure)

_DIMENSION_TYPES: dict[str, str] = {
    "country": "VARCHAR",
    "area_level": "BIGINT",
    "area_id": "VARCHAR",
    "sex": "VARCHAR",
    "age_group": "VARCHAR",
    "calendar_quarter": "VARCHAR",
    "indicator": "VARCHAR",
}
_COLUMN_TYPES: dict[str, str] = {**_DIMENSION_TYPES, **dict.fromkeys(MEASURES, "DOUBLE")}

# A zero-row SELECT with the right column names and types, used when no Parquet
# files are present so the API still starts and queries just return nothing.
EMPTY_DATASET_SQL = (
    "SELECT " + ", ".join(f"NULL::{_COLUMN_TYPES[col]} AS {col}" for col in (*DIMENSIONS, *MEASURES)) + " WHERE false"
)
