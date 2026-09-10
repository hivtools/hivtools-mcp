# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "docopt-ng>=0.9",
#     "polars>=1.0",
# ]
# ///
"""Build a Parquet dataset from a directory of Naomi model output zips.

For every ``*.zip`` in the given directory this reads ``indicators.csv`` straight
from the archive (no need to unzip it first), keeps a fixed subset of columns,
and appends the rows to a Hive-partitioned Parquet dataset keyed by ``country``
(the ISO3 code taken from the national, area_level 0, row):

    naomi-data/
      country=MWI/00000000.parquet
      country=ZWE/00000000.parquet
      ...

Query it with partition pruning, e.g.
``pl.scan_parquet("naomi-data/").filter(pl.col("country") == "MWI")``.

Re-running replaces each country's partition, so it is safe to run repeatedly.

Usage:
  extract_indicators.py <dir> [--out-dir=<path>]
  extract_indicators.py (-h | --help)

Arguments:
  <dir>              Directory of Naomi output zips, each containing indicators.csv.

Options:
  -h --help          Show this help and exit.
  --out-dir=<path>   Root of the partitioned dataset to write into. Defaults to
                     "naomi-data/" next to this script.
"""

from __future__ import annotations

import shutil
import sys
import zipfile
from io import BytesIO
from pathlib import Path

import polars as pl
from docopt import docopt

MEMBER = "indicators.csv"

COLUMNS = [
    "area_level",
    "area_id",
    "sex",
    "age_group",
    "calendar_quarter",
    "indicator",
    "mean",
    "se",
    "median",
    "mode",
    "lower",
    "upper",
]


def read_indicators(zip_path: Path) -> pl.DataFrame:
    """Read the selected columns of indicators.csv from the zip."""
    with zipfile.ZipFile(zip_path) as archive:
        if MEMBER not in archive.namelist():
            sys.exit(f"error: {zip_path} does not contain {MEMBER}")
        raw = archive.read(MEMBER)
    return pl.read_csv(BytesIO(raw), columns=COLUMNS).select(COLUMNS)


def national_iso3(df: pl.DataFrame, zip_path: Path) -> str:
    """ISO3 code from the area_id of the national (area_level 0) row."""
    national = df.filter(pl.col("area_level") == 0)
    if national.is_empty():
        sys.exit(f"error: {zip_path} has no area_level 0 row to read the country from")
    return str(national["area_id"][0]).upper()


def write_partition(df: pl.DataFrame, country: str, out_dir: Path) -> None:
    """Replace this country's partition; other countries are left untouched."""
    partition = out_dir / f"country={country}"
    if partition.exists():
        shutil.rmtree(partition)
    out_dir.mkdir(parents=True, exist_ok=True)
    df.with_columns(pl.lit(country).alias("country")).select(["country", *COLUMNS]).write_parquet(
        out_dir, partition_by="country"
    )


def main() -> None:
    args = docopt(__doc__)

    src = Path(args["<dir>"])
    if not src.is_dir():
        sys.exit(f"error: not a directory: {src}")
    zips = sorted(src.glob("*.zip"))
    if not zips:
        sys.exit(f"error: no .zip files in {src}")

    out_dir = Path(args["--out-dir"]) if args["--out-dir"] else Path(__file__).parent / "naomi-data"

    for zip_path in zips:
        df = read_indicators(zip_path)
        country = national_iso3(df, zip_path)
        write_partition(df, country, out_dir)
        print(f"{zip_path.name}: wrote {len(df):,} rows to {out_dir / f'country={country}'}")


if __name__ == "__main__":
    main()
