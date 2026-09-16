# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "docopt-ng>=0.9",
#     "polars>=1.0",
#     "pyyaml>=6",
# ]
# ///
"""Build the Parquet dataset from the model outputs listed in datasets.yaml files.

Each ``datasets.yaml`` names, per country, the files to build from (see
``raw-data/datasets.yaml`` for the format). Three sources are understood:

- **naomi** (required): the Naomi model output zip. Read here, straight from the
  archive.
- **spectrum** (optional): the Spectrum ``.pjnz`` - the national projection,
  one value a year.
- **shipp** (optional): the SHIPP workbook - Naomi's adults broken down by
  behavioural risk group.

Spectrum and SHIPP are read by ``extract_spectrum_shipp.R``, which this script
runs for any country that lists them: reading a PJNZ needs SpectrumUtils, which
is R only. Only countries that list one need R installed.

For every source this writes:

**A fact table** (``facts/``) - one row per estimate, with the label and
sort-order columns joined on. Denormalising the labels costs about 0.1% on disk,
because Parquet dictionary-encodes the repeated strings, and it saves a join on
every request. Sort orders come along so age bands and areas can be ordered
sensibly with a plain ``ORDER BY`` rather than a join. Every row has a
``risk_group``; for Naomi and Spectrum it is always ``all``.

**Dimension tables** (``dim_area/``, ``dim_age_group/``, ``dim_period/``,
``dim_indicator/``, ``dim_risk_group/``) - for Naomi, the zip's ``meta_*.csv``
files as-is. These are the source of truth for labels, and they are what search
scans (70 area rows, not 400,000) and what the API reads when a query matches no
rows and it has to explain why. Spectrum and SHIPP reuse Naomi's areas and age
groups, and add only the periods, indicators and risk groups they bring.

**A manifest** (``manifest.json``) - per country, how its figures should be
described and just enough provenance to identify each source: the file's name and
SHA-256, the Naomi version, and whether this is demonstration data. The zip's
``info/`` directory holds far more (67 package versions, 40-odd fit options, the
input files and their checksums); none of it is read downstream, and the hash
identifies the archive it can all be recovered from, so copying it into the
manifest would only create a second thing to keep in step. Options and packages
are still *read* here - they are how the Naomi version and the demo status are
worked out - they are just not stored.

Everything is partitioned by ``country`` (the ISO3 code from the national,
area_level 0, row) and ``source``, so re-running replaces one country's source
and leaves the rest:

    naomi-data/
      manifest.json
      facts/country=MWI/source=naomi/*.parquet
      facts/country=TZA/source=spectrum/*.parquet
      dim_area/country=MWI/source=naomi/*.parquet
      ...

Usage:
  extract_indicators.py <datasets>... [--out-dir=<path>]
  extract_indicators.py (-h | --help)

Arguments:
  <datasets>         datasets.yaml files listing the countries to build.

Options:
  -h --help          Show this help and exit.
  --out-dir=<path>   Root of the partitioned dataset to write into. Defaults to
                     "naomi-data/" next to this script.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from docopt import docopt

INDICATORS = "indicators.csv"
SOURCE = "naomi"
R_EXTRACTOR = Path(__file__).parent / "extract_spectrum_shipp.R"
R_SOURCES = ("spectrum", "shipp")

FACT_COLUMNS = [
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

# Naomi estimates are for the whole population. SHIPP splits it by behavioural
# risk group, so every row needs one; this is the total SHIPP's groups add up to.
# extract_spectrum_shipp.R writes the same row.
ALL_RISK_GROUPS = pl.DataFrame(
    {"risk_group": ["all"], "risk_group_label": ["All"], "risk_group_sort_order": [0]},
    schema={"risk_group": pl.String, "risk_group_label": pl.String, "risk_group_sort_order": pl.Int64},
)

# Each dimension table, and the columns joined onto the fact table from it. The
# join columns are deliberately few: enough to label a row and order it, no more.
DIMENSIONS = {
    "dim_area": {
        "member": "meta_area.csv",
        "key": "area_id",
        "join": ["area_name", "area_level_label", "area_sort_order"],
        "integers": ["area_level", "area_sort_order"],
    },
    "dim_age_group": {
        "member": "meta_age_group.csv",
        "key": "age_group",
        "join": ["age_group_label", "age_group_sort_order"],
        "integers": ["age_group_start", "age_group_sort_order"],
    },
    "dim_period": {
        "member": "meta_period.csv",
        "key": "calendar_quarter",
        "join": ["quarter_label"],
        "integers": ["quarter_id"],
    },
    "dim_indicator": {
        "member": "meta_indicator.csv",
        "key": "indicator",
        "join": ["indicator_label"],
        "integers": ["indicator_sort_order"],
    },
}


def read_member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    """Bytes of a zip member, or None when the archive does not contain it."""
    try:
        return archive.read(name)
    except KeyError:
        return None


def read_meta(archive: zipfile.ZipFile, member: str, integers: list[str]) -> pl.DataFrame:
    """A meta_*.csv as a DataFrame, everything string except the named integers.

    Read as text first because some columns are not cleanly typed - age_group_span
    carries "Inf" for open-ended bands - and only the columns we sort by need to
    be numeric.
    """
    raw = read_member(archive, member)
    if raw is None:
        sys.exit(f"error: archive does not contain {member}")
    frame = pl.read_csv(BytesIO(raw), infer_schema_length=0)
    present = [column for column in integers if column in frame.columns]
    return frame.with_columns([pl.col(column).cast(pl.Int64, strict=False) for column in present])


def build_facts(indicators: pl.DataFrame, dims: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """Join the label and sort-order columns onto the indicator rows."""
    facts = indicators
    for name, spec in DIMENSIONS.items():
        key = spec["key"]
        lookup = dims[name].select([key, *spec["join"]])
        facts = facts.join(lookup, on=key, how="left")
    return facts.join(ALL_RISK_GROUPS, how="cross")


def national_row(indicators: pl.DataFrame, zip_path: Path) -> tuple[str, str]:
    """ISO3 code and display name from the national (area_level 0) row."""
    national = indicators.filter(pl.col("area_level") == 0)
    if national.is_empty():
        sys.exit(f"error: {zip_path} has no area_level 0 row to read the country from")
    return str(national["area_id"][0]).upper(), str(national["area_name"][0])


def demo_signals(facts: pl.DataFrame, country_label: str, options: dict[str, Any]) -> list[str]:
    """Independent hints that this is demonstration rather than real data.

    Derived rather than configured: a deployment flag can be set wrong, and
    presenting synthetic figures as national estimates is the worst thing this
    server could do.
    """
    signals = []
    if facts["area_id"].str.ends_with("_demo").any():
        signals.append("area_id values end in '_demo'")
    if "demo" in country_label.lower():
        signals.append(f"country name is {country_label!r}")
    surveys = {str(value) for key, value in options.items() if key.startswith("survey_")}
    demo_surveys = sorted(name for name in surveys if "DEMO" in name.upper())
    if demo_surveys:
        signals.append(f"survey identifiers {demo_surveys}")
    return signals


def naomi_version(archive: zipfile.ZipFile) -> str | None:
    """The naomi package version from info/packages.csv, if it is recorded."""
    raw = read_member(archive, "info/packages.csv")
    if raw is None:
        return None
    frame = pl.read_csv(BytesIO(raw), infer_schema_length=0).filter(pl.col("name") == "naomi")
    return None if frame.is_empty() else str(frame["version"][0])


def fit_options(archive: zipfile.ZipFile) -> dict[str, Any]:
    """The fit options, read for demo detection but not stored."""
    raw = read_member(archive, "info/options.yml")
    return (yaml.safe_load(raw) or {}) if raw is not None else {}


def file_digest(path: Path) -> str:
    """SHA-256 of a source file.

    This is what makes the rest of info/ droppable: anything not stored here -
    package versions, fit options, input checksums - is recoverable from the
    archive this hash identifies, without keeping a stale copy of it alongside
    the data.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


SOURCE_DESCRIPTIONS = {
    "naomi": "Naomi {version} subnational estimates",
    "spectrum": "Spectrum national projection",
    "shipp": "SHIPP estimates by behavioural risk group",
}


def describe(source: str, label: str, is_demo: bool, version: str | None = None) -> str:
    """How a source's figures should be described, which is what the API serves as ``source``."""
    what = SOURCE_DESCRIPTIONS[source].format(version=version or "").replace("  ", " ")
    text = f"{label} - {what}"
    return f"{text} - DEMONSTRATION data, not official estimates" if is_demo else text


def source_entry(path: Path, description: str, **extra: Any) -> dict[str, Any]:
    return {"description": description, "file": path.name, "sha256": file_digest(path), **extra}


def build_manifest(
    archive: zipfile.ZipFile,
    inputs: dict[str, Path],
    facts: pl.DataFrame,
    country_label: str,
    label: str | None,
) -> dict[str, Any]:
    """Provenance for one country: what produced each source, and whether it is real."""
    version = naomi_version(archive)
    is_demo = bool(demo_signals(facts, country_label, fit_options(archive)))
    label = label or country_label

    sources = {
        "naomi": source_entry(inputs["naomi"], describe("naomi", label, is_demo, version), naomi_version=version)
    }
    for source in R_SOURCES:
        if source in inputs:
            sources[source] = source_entry(inputs[source], describe(source, label, is_demo))

    return {
        "country_label": country_label,
        "label": label,
        "is_demo": is_demo,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
    }


def write_partition(frame: pl.DataFrame, table: str, country: str, out_dir: Path) -> None:
    """Replace this country's Naomi partition of one table; everything else is untouched."""
    root = out_dir / table
    partition = root / f"country={country}" / f"source={SOURCE}"
    if partition.exists():
        shutil.rmtree(partition)
    root.mkdir(parents=True, exist_ok=True)
    frame.with_columns(pl.lit(country).alias("country"), pl.lit(SOURCE).alias("source")).write_parquet(
        root, partition_by=["country", "source"]
    )


def update_manifest(manifest: dict[str, Any], country: str, out_dir: Path) -> None:
    """Merge one country into the shared manifest, leaving other countries alone."""
    path = out_dir / "manifest.json"
    document: dict[str, Any] = {"countries": {}}
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        document.setdefault("countries", {})
    document["countries"][country] = manifest
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_r_extractor(country: str, inputs: dict[str, Path], out_dir: Path) -> None:
    """Write the Spectrum and SHIPP partitions for one country."""
    rscript = shutil.which("Rscript")
    if rscript is None:
        sys.exit(f"error: {country} lists {[s for s in R_SOURCES if s in inputs]}, which need Rscript on the PATH")
    args = [rscript, str(R_EXTRACTOR), f"--country={country}", f"--naomi={inputs['naomi']}", f"--out-dir={out_dir}"]
    args += [f"--{source}={inputs[source]}" for source in R_SOURCES if source in inputs]
    subprocess.run(args, check=True)  # noqa: S603 - arguments come from the datasets file, not a request


def process(country: str, inputs: dict[str, Path], label: str | None, out_dir: Path) -> None:
    zip_path = inputs["naomi"]
    with zipfile.ZipFile(zip_path) as archive:
        raw = read_member(archive, INDICATORS)
        if raw is None:
            sys.exit(f"error: {zip_path} does not contain {INDICATORS}")
        indicators = pl.read_csv(BytesIO(raw), columns=FACT_COLUMNS).select(FACT_COLUMNS)

        dims = {name: read_meta(archive, spec["member"], spec["integers"]) for name, spec in DIMENSIONS.items()}
        facts = build_facts(indicators, dims)
        found, country_label = national_row(facts, zip_path)
        if found != country:
            sys.exit(f"error: {zip_path} is listed under {country} but its national row is {found}")
        manifest = build_manifest(archive, inputs, facts, country_label, label)

    write_partition(facts, "facts", country, out_dir)
    for name, frame in {**dims, "dim_risk_group": ALL_RISK_GROUPS}.items():
        write_partition(frame, name, country, out_dir)
    status = " [DEMO data]" if manifest["is_demo"] else ""
    print(f"{zip_path.name}: {facts.height:,} naomi rows for {country}{status} -> {out_dir}", flush=True)

    if any(source in inputs for source in R_SOURCES):
        run_r_extractor(country, inputs, out_dir)
    update_manifest(manifest, country, out_dir)


def read_datasets(path: Path) -> dict[str, dict[str, Any]]:
    """The countries a datasets.yaml lists, with input paths resolved against it."""
    if not path.is_file():
        sys.exit(f"error: not a file: {path}")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    countries = {}
    for country, spec in document.items():
        if "naomi" not in spec:
            sys.exit(f"error: {path}: {country} has no naomi output; every country needs one")
        unknown = set(spec) - {"label", "naomi", *R_SOURCES}
        if unknown:
            sys.exit(f"error: {path}: {country} has unknown keys {sorted(unknown)}")
        inputs = {source: path.parent / spec[source] for source in ("naomi", *R_SOURCES) if source in spec}
        missing = [str(file) for file in inputs.values() if not file.is_file()]
        if missing:
            sys.exit(f"error: {path}: {country} lists files that do not exist: {missing}")
        countries[str(country)] = {"inputs": inputs, "label": spec.get("label")}
    return countries


def main() -> None:
    args = docopt(__doc__)
    out_dir = Path(args["--out-dir"]) if args["--out-dir"] else Path(__file__).parent / "naomi-data"

    countries: dict[str, dict[str, Any]] = {}
    for datasets in args["<datasets>"]:
        for country, spec in read_datasets(Path(datasets)).items():
            if country in countries:
                sys.exit(f"error: {country} is listed in more than one datasets file")
            countries[country] = spec

    for country, spec in countries.items():
        process(country, spec["inputs"], spec["label"], out_dir)


if __name__ == "__main__":
    main()
