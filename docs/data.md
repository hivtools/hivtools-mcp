# Data

The API is served from one Parquet dataset built from three kinds of model output.
Each row has a `source` saying which it came from:

| `source` | Input | What it adds |
| --- | --- | --- |
| `naomi` | Naomi output zip | Subnational estimates for a few recent quarters, with uncertainty |
| `spectrum` | Spectrum `.pjnz` | National estimates for every year since 1970, including projections |
| `shipp` | SHIPP workbook `.xlsx` | Adults 15-49 split by behavioural `risk_group` (female sex workers, MSM, PWID, ...) |

## Building the dataset

```bash
make data
```

That deletes `data-prep/naomi-data/` and rebuilds it from the countries listed in
[`data-prep/raw-data/datasets.yaml`](https://github.com/hivtools/hivtools-mcp/blob/main/data-prep/raw-data/datasets.yaml)
(the public demo data) and, if it is checked out, `data-prep/private-data/datasets.yaml`.
The API reads the dataset from `data-prep/naomi-data/` by default; point it
elsewhere with `HIVTOOLS_MCP_NAOMI_DATA_DIR`.

Each `datasets.yaml` names a country's input files and how its figures should be
described:

```yaml
TZA:
  label: Tanzania estimates 2026   # optional; defaults to the country's name
  naomi: TZA/naomi_outputs.zip     # required
  spectrum: TZA/national.pjnz      # optional
  shipp: TZA/shipp.xlsx            # optional
```

## Private data

Data that is not public lives in the private repo
[hivtools/hivtools-mcp-data](https://github.com/hivtools/hivtools-mcp-data)
(ask a maintainer for access). It is a separate git repo, cloned into this one at
`data-prep/private-data/`, which is gitignored here - as is everything in
`raw-data/` except the demo inputs. This repo is public: never commit private
inputs or anything built from them, and never print them in CI.

### Setting it up

From the root of this repo:

```bash
git clone git@github.com:hivtools/hivtools-mcp-data.git data-prep/private-data
make r-deps   # once: the R packages the Spectrum/SHIPP extractor needs
make data     # builds the demo data and the private data
make test     # also checks the knowledge files against the private data
```

Without the clone, `make data` builds only the demo data, and the tests that
need private data are skipped.

### Updating it

To change the data - update a file, or add a country - work in the clone and push
it to the data repo like any other git repo:

```bash
cd data-prep/private-data
git pull
# add or replace files under <ISO3>/, and list them in datasets.yaml
cd ../..
make data && make test   # check it builds before pushing
cd data-prep/private-data
git add . && git commit -m "Update TZA inputs" && git push
```

Pushed data goes live with the next release, which builds from the data repo's
default branch (or `DATA_REPO_REF`). See
[Deploying](https://github.com/hivtools/hivtools-mcp#deploying).

## How it is built

[`data-prep/extract_indicators.py`](https://github.com/hivtools/hivtools-mcp/blob/main/data-prep/extract_indicators.py)
is the entry point. It reads each Naomi zip without unzipping it, and runs
[`data-prep/extract_spectrum_shipp.R`](https://github.com/hivtools/hivtools-mcp/blob/main/data-prep/extract_spectrum_shipp.R)
for any country that lists Spectrum or SHIPP inputs (reading a PJNZ needs
[SpectrumUtils](https://github.com/rlglaubius/SpectrumUtils), which is R only; a
country with only a Naomi zip needs no R). Spectrum and SHIPP reuse the Naomi
output's areas, age groups and indicator IDs - an indicator from two sources is
the same quantity, estimated by two models - and are shaped to be queried the same
way:

- **Spectrum**'s single-year ages are summed into every Naomi age group, and
  `both` is added for sex. Its values are dated to Q4 of each year (Q2 for files
  from before Spectrum 6.2), as Naomi itself does.
- **SHIPP** is read from its five-year age bands only. Wider age groups, `both`,
  and every area level above the districts are summed from them, and rates are
  recomputed from the sums. Its quarter is the Naomi round the workbook was built
  from, read from its "Model inputs" sheet. Within a sex its risk groups are
  mutually exclusive and add up to the whole population; Naomi and Spectrum rows
  have the single risk group `all`.

See `--help` for running the extractor directly.

### Output layout

```
data-prep/naomi-data/
  manifest.json                                    provenance, keyed by country
  facts/country=TZA/source=naomi/00000000.parquet  estimates + labels + sort keys
  facts/country=TZA/source=spectrum/part-0.parquet
  dim_area/country=TZA/source=naomi/...            meta_area.csv
  dim_age_group/...                                meta_age_group.csv
  dim_period/...                                   meta_period.csv, plus Spectrum's years
  dim_indicator/...                                meta_indicator.csv, plus aids_deaths
  dim_risk_group/...                               risk group labels
```

The labels are denormalised onto the fact table as well as kept in the dimension
tables. That costs about 0.1% on disk - Parquet dictionary-encodes the repeated
strings - and saves a join on every request. The dimension tables stay because
they are the source of truth for labels, they are what a name lookup scans (70
area rows rather than 400,000), and they are where the API reads labels from when
a query matches no rows and has to explain what was asked for.

`manifest.json` records, per country, how its figures should be described (the
`label`) and, per source, the input file, its SHA-256 and a description the API
serves as `meta.source`. It also records whether the data is a demonstration
dataset. That is derived from the Naomi output rather than configured, so it
cannot be set wrong in a deployment.

Query it with partition pruning, e.g.
`pl.scan_parquet("data-prep/naomi-data/facts/").filter(pl.col("country") == "MWI")`,
or point DuckDB at the same path (with `union_by_name`: the sources are written by
different tools).

## How data gets into the Docker image

The dataset is baked into the image; nothing is fetched at runtime. There are two
ways to build it:

```bash
docker build -t hivtools-mcp .   # the public demo data only
make docker                      # whatever `make data` builds, private data included
```

A plain `docker build` builds the demo dataset from `data-prep/raw-data/` in a
first stage, so it needs nothing but Docker. `make docker` builds the dataset
outside Docker instead - private inputs need R - and passes it in as a named
build context (`--build-context data=data-prep/naomi-data`), which replaces that
stage. The release workflow does the same. Either way only the built Parquet
reaches the image, and the private inputs never enter the build context.

The image refuses to start without `HIVTOOLS_MCP_API_TOKEN`, because the data
baked into it may not be public. For a throwaway local run,
`-e HIVTOOLS_MCP_REQUIRE_AUTH=false` turns that off.

To serve a different dataset without rebuilding, mount it and override the path:

```bash
docker run -p 80:80 -v "$PWD/data-prep/naomi-data:/data:ro" -e HIVTOOLS_MCP_NAOMI_DATA_DIR=/data -e HIVTOOLS_MCP_API_TOKEN=some-token hivtools-mcp
```
