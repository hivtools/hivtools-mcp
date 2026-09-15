"""Explaining why a query matched nothing.

An empty result is the worst failure this API has, because it is indistinguishable
from a true negative. A caller that asks for an indicator in a quarter it does not
cover, or guesses an ID that does not exist, gets ``{"total": 0, "data": []}`` and
no way to tell "there is no such thing" from "there is no data for that" from "you
made a typo". In practice that means either reporting "no data" - which is wrong -
or retrying at random.

Two questions answer nearly every case, in this order:

1. Is one of the values not in the data at all? Then it is an unknown ID, and the
   search index can say what was probably meant.
2. Are all the values real, but the combination empty? Then some single filter is
   responsible, and dropping it in turn finds which - along with the values that
   *would* have worked.

Cost is a handful of counts over a filtered scan, paid only on the empty path,
which is exactly where latency is worth spending.
"""

from collections.abc import Sequence
from typing import Any

import duckdb

from app.query import Filters, count_rows, distinct_values
from app.schema import DIMENSION_LOOKUPS
from app.search import Entry

# Dimensions whose values are opaque enough that a near-miss is worth suggesting.
# `sex` and `area_level` have a handful of values each, so listing them all is
# more useful than guessing which was meant.
SEARCHABLE = {"indicator": "indicator", "area_id": "area", "age_group": "age_group"}

SUGGESTIONS = 3
SUGGESTION_FLOOR = 60.0
MAX_LISTED = 12


def _rows(count: int) -> str:
    return f"{count} row" if count == 1 else f"{count} rows"


def _listed(values: Sequence[Any]) -> str:
    shown = ", ".join(str(value) for value in values[:MAX_LISTED])
    return f"{shown}, ..." if len(values) > MAX_LISTED else shown


def _in_codelist(cursor: duckdb.DuckDBPyConnection, column: str, values: Sequence[Any]) -> set[Any]:
    """Which of ``values`` the dimension actually defines.

    Membership is the dimension table's question, not the fact table's. A code
    that exists but has no rows is a valid ask with no data - the second case
    below - and calling it unknown would suggest the value as a correction for
    itself.
    """
    lookup = DIMENSION_LOOKUPS.get(column)
    if lookup is None:
        # country and sex have no dimension table; the facts are the codelist.
        return set(distinct_values(cursor, column, {column: values}))
    view, key, _ = lookup
    placeholders = ", ".join("?" for _ in values)
    sql = f'SELECT DISTINCT "{key}" FROM {view} WHERE "{key}" IN ({placeholders})'  # noqa: S608
    return {row[0] for row in cursor.execute(sql, list(values)).fetchall()}


def unknown_values(cursor: duckdb.DuckDBPyConnection, filters: Filters) -> dict[str, list[Any]]:
    """Requested values the dataset does not define at all, per dimension."""
    unknown = {}
    for column, values in filters.items():
        if not values:
            continue
        defined = _in_codelist(cursor, column, values)
        missing = [value for value in values if value not in defined]
        if missing:
            unknown[column] = missing
    return unknown


def _suggest(index: Sequence[Entry], field: str, value: str) -> list[Entry]:
    scored = sorted(
        ((entry.score(str(value)), entry) for entry in index if entry.field == field),
        key=lambda pair: -pair[0],
    )
    return [entry for score, entry in scored[:SUGGESTIONS] if score >= SUGGESTION_FLOOR]


def _unknown_message(
    cursor: duckdb.DuckDBPyConnection,
    column: str,
    values: Sequence[Any],
    index: Sequence[Entry],
) -> str:
    value = values[0]
    field = SEARCHABLE.get(column)
    if field is None:
        # Few enough values that listing them beats guessing which was meant.
        valid = distinct_values(cursor, column, {})
        return f"Unknown {column} {value!r}. Valid values are: {_listed(valid)}."
    matches = _suggest(index, field, value)
    if not matches:
        return f"Unknown {column} {value!r}. Use search(field={field!r}) to find the right one."
    named = ", ".join(f"{entry.id} ({entry.label})" for entry in matches)
    return f"Unknown {column} {value!r}. Did you mean: {named}? Use search(field={field!r})."


def _blockers(cursor: duckdb.DuckDBPyConnection, filters: Filters) -> list[tuple[int, str]]:
    """Filters that, dropped on their own, would have returned rows.

    Ordered by how few rows that leaves. The narrowest is reported: when both the
    quarter and the indicator would unblock a query, the quarter is almost always
    the mistake, and it is the one whose removal leaves least.
    """
    found = []
    for column in filters:
        without = {name: values for name, values in filters.items() if name != column}
        matches = count_rows(cursor, without)
        if matches:
            found.append((matches, column))
    return sorted(found)


def _blocking_message(cursor: duckdb.DuckDBPyConnection, filters: Filters, blockers: list[tuple[int, str]]) -> str:
    matches, column = blockers[0]
    without = {name: values for name, values in filters.items() if name != column}
    available = distinct_values(cursor, column, without)
    asked = _listed(list(filters[column] or []))
    sentence = f"Dropping {column} would return {_rows(matches)}."
    if available:
        sentence += f" Under your other filters {column} can be: {_listed(available)} - you asked for {asked}."
    others = [name for _, name in blockers[1:]]
    if others:
        sentence += f" (Relaxing {' or '.join(others)} would also return rows.)"
    return sentence


def diagnose(
    cursor: duckdb.DuckDBPyConnection,
    filters: Filters,
    index: Sequence[Entry],
) -> str | None:
    """A sentence or two saying why nothing matched, or None if something did."""
    applied: Filters = {column: values for column, values in filters.items() if values}
    if not applied:
        return "The dataset is empty - no data has been loaded."

    unknown = unknown_values(cursor, applied)
    if unknown:
        return " ".join(_unknown_message(cursor, column, values, index) for column, values in unknown.items())

    blockers = _blockers(cursor, applied)
    if blockers:
        return "No rows matched. " + _blocking_message(cursor, applied, blockers)
    return (
        "No rows matched. Every value is valid on its own and no single filter explains it, "
        "so this combination of filters has no data. Relax more than one of them."
    )
