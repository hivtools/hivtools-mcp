"""The column set of the indicators dataset, and how dimensions map to labels.

Centralised so the query builder, the empty-dataset fallback views and the
response builder cannot drift apart, and so every column name that reaches SQL
comes from here rather than from a request.
"""

from typing import Literal, get_args

# Categorical columns: queryable as filters, and the grain of a fact row.
# DuckDB view names created by app.database over the Parquet dataset.
FACT_VIEW = "indicators"

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

# Human labels denormalised onto the fact table at data-prep time. `country` and
# `sex` are absent deliberately: their codes ('MWI', 'both') are already
# readable, so a label column would be repetition.
DIMENSION_LABELS: dict[str, str] = {
    "area_level": "area_level_label",
    "area_id": "area_name",
    "age_group": "age_group_label",
    "calendar_quarter": "quarter_label",
    "indicator": "indicator_label",
}
LABEL_COLUMNS: tuple[str, ...] = tuple(DIMENSION_LABELS.values())

# Where to find a label when the query matched no rows, so none is available to
# read off the results: dimension -> (view, key column, label column).
DIMENSION_LOOKUPS: dict[str, tuple[str, str, str]] = {
    "area_level": ("dim_area", "area_level", "area_level_label"),
    "area_id": ("dim_area", "area_id", "area_name"),
    "age_group": ("dim_age_group", "age_group", "age_group_label"),
    "calendar_quarter": ("dim_period", "calendar_quarter", "quarter_label"),
    "indicator": ("dim_indicator", "indicator", "indicator_label"),
}

# Sort keys from the dimension tables. Ordering by these rather than by the codes
# puts age bands in age order (a code sort drops 'all ages' in the middle of the
# five-year bands) and areas in their published order.
SORT_COLUMNS: dict[str, str] = {
    "area_id": "area_sort_order",
    "age_group": "age_group_sort_order",
}
ORDER_BY: tuple[str, ...] = (
    "country",
    "area_level",
    "area_sort_order",
    "sex",
    "age_group_sort_order",
    "calendar_quarter",
    "indicator",
)

_DIMENSION_TYPES: dict[str, str] = {
    "country": "VARCHAR",
    "area_level": "BIGINT",
    "area_id": "VARCHAR",
    "sex": "VARCHAR",
    "age_group": "VARCHAR",
    "calendar_quarter": "VARCHAR",
    "indicator": "VARCHAR",
}
_FACT_TYPES: dict[str, str] = {
    **_DIMENSION_TYPES,
    **dict.fromkeys(MEASURES, "DOUBLE"),
    **dict.fromkeys(LABEL_COLUMNS, "VARCHAR"),
    **dict.fromkeys(SORT_COLUMNS.values(), "BIGINT"),
}

# Only the dimension-table columns the API actually reads. A real dataset carries
# more (centroids, spectrum codes); these views just have to satisfy the lookups.
_DIM_TYPES: dict[str, dict[str, str]] = {
    "dim_area": {
        "country": "VARCHAR",
        "area_id": "VARCHAR",
        "area_name": "VARCHAR",
        "area_level": "BIGINT",
        "area_level_label": "VARCHAR",
        "parent_area_id": "VARCHAR",
        "area_sort_order": "BIGINT",
    },
    "dim_age_group": {
        "country": "VARCHAR",
        "age_group": "VARCHAR",
        "age_group_label": "VARCHAR",
        "age_group_sort_order": "BIGINT",
    },
    "dim_period": {
        "country": "VARCHAR",
        "calendar_quarter": "VARCHAR",
        "quarter_label": "VARCHAR",
    },
    "dim_indicator": {
        "country": "VARCHAR",
        "indicator": "VARCHAR",
        "indicator_label": "VARCHAR",
        "description": "VARCHAR",
    },
}


def _empty_select(columns: dict[str, str]) -> str:
    """A zero-row SELECT with the right names and types, for a missing table."""
    return "SELECT " + ", ".join(f"NULL::{type_} AS {name}" for name, type_ in columns.items()) + " WHERE false"


FACT_COLUMNS: tuple[str, ...] = (*DIMENSIONS, *MEASURES, *LABEL_COLUMNS, *SORT_COLUMNS.values())
EMPTY_DATASET_SQL = _empty_select({name: _FACT_TYPES[name] for name in FACT_COLUMNS})
EMPTY_DIM_SQL: dict[str, str] = {name: _empty_select(columns) for name, columns in _DIM_TYPES.items()}
