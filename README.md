# hivtools-mcp

[![Release](https://img.shields.io/github/v/release/hivtools/hivtools-mcp)](https://img.shields.io/github/v/release/hivtools/hivtools-mcp)
[![Build status](https://img.shields.io/github/actions/workflow/status/hivtools/hivtools-mcp/main.yml?branch=main)](https://github.com/hivtools/hivtools-mcp/actions/workflows/main.yml?query=branch%3Amain)
[![codecov](https://codecov.io/gh/hivtools/hivtools-mcp/branch/main/graph/badge.svg)](https://codecov.io/gh/hivtools/hivtools-mcp)
[![Commit activity](https://img.shields.io/github/commit-activity/m/hivtools/hivtools-mcp)](https://img.shields.io/github/commit-activity/m/hivtools/hivtools-mcp)
[![License](https://img.shields.io/github/license/hivtools/hivtools-mcp)](https://img.shields.io/github/license/hivtools/hivtools-mcp)

API and MCP server for hivtools data

- **Github repository**: <https://github.com/hivtools/hivtools-mcp/>
- **Documentation** <https://hivtools.github.io/hivtools-mcp/>

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Running

### Setup

Install the dependencies into a local virtual environment:

```bash
uv sync
```

### Running the API & MCP

For local development (reloads on file changes):

```bash
make dev
# equivalent to: uv run fastapi dev app/main.py
```

This serves the API at <http://127.0.0.1:8000>, with interactive docs at <http://127.0.0.1:8000/docs>.

The API reads the Parquet dataset described in [Data preparation](#data-preparation);
build that first, or `/data` will just return nothing. By default it looks in
`data-prep/naomi-data/` - point it elsewhere with `HIVTOOLS_MCP_NAOMI_DATA_DIR`.

The MCP server is avaialble at `/mcp`

### With Docker

```bash
docker build -t hivtools-mcp .
docker run -p 80:80 hivtools-mcp
```

The API is then served at <http://127.0.0.1:80>. The build extracts the demo
dataset from the zips in `data-prep/raw-data/` (a first build stage runs
[`data-prep/extract_indicators.py`](data-prep/extract_indicators.py)) and bakes it
into the image, so a plain `docker build` always ships whatever is in
`data-prep/raw-data/` at build time. To serve a different dataset instead, mount
it and override the path:

```bash
docker run -p 80:80 -v "$PWD/data-prep/naomi-data:/data:ro" -e HIVTOOLS_MCP_NAOMI_DATA_DIR=/data hivtools-mcp
```

## Data preparation

The API is served from a Parquet dataset built from Naomi model output zips.
[`data-prep/extract_indicators.py`](data-prep/extract_indicators.py) takes a
directory of zips, reads each one without unzipping, and writes a fact table, the
dimension tables that label it, and a provenance manifest - all Hive-partitioned
by `country`.

```bash
uv run --script data-prep/extract_indicators.py data-prep/raw-data
```

Every `*.zip` in the directory is processed. Each country's ISO3 code is read
from the national (`area_level 0`) row in its data. The result is:

```
data-prep/naomi-data/
  manifest.json                          provenance, keyed by country
  facts/country=MWI/00000000.parquet     indicators.csv + labels + sort keys
  dim_area/country=MWI/00000000.parquet  meta_area.csv
  dim_age_group/country=MWI/...          meta_age_group.csv
  dim_period/country=MWI/...             meta_period.csv
  dim_indicator/country=MWI/...          meta_indicator.csv
```

The labels are denormalised onto the fact table as well as kept in the dimension
tables. That costs about 0.1% on disk - Parquet dictionary-encodes the repeated
strings - and saves a join on every request. The dimension tables stay because
they are the source of truth for labels, they are what a name lookup scans (70
area rows rather than 400,000), and they are where the API reads labels from when
a query matches no rows and has to explain what was asked for.

`manifest.json` records, per country, the Naomi version that produced the output,
the input files it was built from (including the PJNZ), the fit options, and
whether the data is a demonstration dataset. That last one is derived from the
data rather than configured, so it cannot be set wrong in a deployment.

Query it with partition pruning, e.g.
`pl.scan_parquet("data-prep/naomi-data/facts/").filter(pl.col("country") == "MWI")`,
or point DuckDB at the same path. Re-running replaces each country's partition
and merges that country into the manifest, so it is safe to repeat. Change the
output root with `--out-dir`; see `--help` for details.

## API

Full schema and a try-it console are at `/docs` when the API is running (disabled in
production - set `HIVTOOLS_MCP_ENABLE_DOCS=false`; `/openapi.json` stays available).

### `GET /`, `GET /version`

Both return `{"name": "hivtools-mcp", "version": "<pyproject version>"}`.

### `GET /health`, `GET /health/ready`

`/health` is a liveness check (`{"status": "ok"}`). `/health/ready` also checks the DuckDB
connection answers a query (`{"status": "ready"}`, or `503`). Used by the Azure Container
Apps probes.

### `GET /search`

Resolves plain-language terms to the IDs `/data` accepts. Nothing in this dataset
has a guessable ID - the treatment gap is `untreated_plhiv_num`, Lilongwe is
`MWI_3_13_demo`, children are `Y000_014` - and guessing returns an empty result
rather than an error, so terms are resolved here first.

One index covers indicators, areas, age groups, age partitions and concepts, each
result tagged with its `field`. Repeat `q` to resolve several terms in one call:

```bash
curl "http://127.0.0.1:8000/search?q=treatment+gap&q=children&q=Lilongwe&country=MWI"
```

| Parameter | Purpose |
| --- | --- |
| `q` | Term to resolve, as a user would phrase it. Repeat for several. |
| `field` | Restrict to `concept`, `indicator`, `area`, `age_group` or `age_partition`. Omit when unsure. |
| `country` | ISO3 code; scopes areas, age groups and coverage to one country. |
| `limit` | Maximum matches per term (default 5). |

The best match comes back in full (`detail: "full"`), the rest as summaries. For a
concept that means the indicators it maps to, their units, the dimension values
they actually have rows for, and the caveats that make an answer correct - all of
which the caller needs *before* querying, not after.

`ambiguous: true` means the top two matches genuinely compete - `Lilongwe` the
district and `Lilongwe` the district+metro area, or `adults` as 15-49 versus 15+.
Both are returned in full so the choice can be made on substance, or put to the
user. Matches of different kinds never compete: a concept and one of the
indicators it names both match "treatment gap", but that is not a choice.

Matching is layered and dependency-free - exact, then token-set (so "burden of
HIV" and "HIV burden" are identical), then word-boundary substring, then difflib
for typos. At a few hundred entries that is microseconds and every score is
explainable; rapidfuzz and DuckDB's FTS extension are the upgrade path if
quality, not speed, becomes the limit.

### `GET /data`

Filtered rows from the indicators dataset as JSON:
`{"meta": { ... }, "total": <n>, "data": [ ... ]}`, where `total` is the number of
rows matching the filters ignoring `limit`/`offset` (so a caller can page and show
"N of total").

When nothing matches, the response carries a `diagnostic` explaining why - either
a value the dataset does not define (with the nearest real ones, via the same
matcher `/search` uses) or a valid combination with no rows, naming the filter
responsible and the values that would have worked.

Dimensions identical across every matching row are hoisted into `meta` and dropped
from the rows - the common query pins five of the six dimensions and varies one,
so repeating them per row is most of the payload and none of the information.
`meta` also carries each dimension's label, the `unit` needed to interpret a value
(`proportion` is a fraction 0-1, so `0.108` means 10.8%), and the `source` of the
estimates. A dimension is only hoisted when it is provably constant across the
whole result, not merely constant on the page in hand.

**Filters** - to match any of several values (`OR`), repeat the parameter
(`?indicator=a&indicator=b`) or comma-separate it (`?indicator=a,b`); different
parameters combine with `AND`:

| Parameter | Notes |
| --- | --- |
| `country` | ISO3, e.g. `MWI` - the partition key, so this is the cheapest filter |
| `area_level` | integer, `0`-`4` |
| `area_id` | e.g. `MWI`, `MWI_1_1_demo` |
| `sex` | `both`, `female`, `male` |
| `age_group` | e.g. `Y015_049` |
| `calendar_quarter` | e.g. `CY2024Q3` |
| `indicator` | e.g. `prevalence`, `art_coverage` |

`age_partition` takes the **name** of an age-group set instead - `five_year_bands`,
`child_adult` - and expands it to that set's age groups. The groups mix a partition
with overlapping aggregates (`Y015_049` and `Y000_999` sit in the same column as
the five-year bands), so a caller assembling a breakdown by hand can easily build
one that overlaps, and nothing in the result would say so. Naming the set instead
means the rows returned tile their population exactly and can be added up.
`search?field=age_partition` lists what is available; it cannot be combined with
`age_group`.

**Shape**

| Parameter | Default | Notes |
| --- | --- | --- |
| `columns` | all six | Which estimate columns to return: `mean`, `se`, `median`, `mode`, `lower`, `upper`. Repeat or comma-separate. This is a projection, not a row filter - the categorical columns above are always returned. |
| `limit` | `1000` | 1-5000 (`HIVTOOLS_MCP_MAX_ROWS`) |
| `offset` | `0` | Results are ordered by the categorical columns, so `limit`/`offset` paginate deterministically. |
| `sig_figs` | `6` (`HIVTOOLS_MCP_RESPONSE_SIG_FIGS`) | Significant figures the measure values are rounded to on the way out, 1-15. The Parquet dataset keeps full model precision; this is presentation only. |

An unknown `columns` value, a non-integer `area_level`, or a `limit`/`sig_figs` outside its
range, is a `422`. `/data` is rate limited per client IP (`HIVTOOLS_MCP_DATA_RATE_LIMIT`,
default `30/minute`); over that is a `429`. Responses carry `Cache-Control: public,
max-age=300` (`HIVTOOLS_MCP_CACHE_MAX_AGE`).

**Examples**

```bash
# HIV prevalence for 15-49s, national level, just the point estimate and CI
curl "http://127.0.0.1:8000/data?indicator=prevalence&area_id=MWI&age_group=Y015_049&columns=mean,lower,upper"

# ART coverage for two countries, women, most recent quarter
curl "http://127.0.0.1:8000/data?indicator=art_coverage&country=MWI,ZWE&sex=female&calendar_quarter=CY2024Q3"
```

### Configuration

Environment variables (or a `.env` file), all prefixed `HIVTOOLS_MCP_`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `HIVTOOLS_MCP_NAOMI_DATA_DIR` | `data-prep/naomi-data` | Root of the Parquet dataset to serve. |
| `HIVTOOLS_MCP_SEARCH_RATE_LIMIT` | `120/minute` | Per-IP limit on `/search`; looser than `/data` since an agent makes several lookups per question. |
| `HIVTOOLS_MCP_DUCKDB_THREADS` | unset (one per core) | Caps threads per query; lower it if many concurrent requests oversubscribe the CPU. |
| `HIVTOOLS_MCP_RESPONSE_SIG_FIGS` | `6` | Default significant figures for measure values in responses; a request can override with `?sig_figs=`. |
| `HIVTOOLS_MCP_MAX_ROWS` | `5000` | Largest page `?limit=` may request. |
| `HIVTOOLS_MCP_DEFAULT_ROWS` | `1000` | Page size when `?limit=` is omitted. |
| `HIVTOOLS_MCP_DATA_RATE_LIMIT` | `30/minute` | Per-IP rate limit on `/data`. |
| `HIVTOOLS_MCP_RATE_LIMIT_ENABLED` | `true` | Master switch for the rate limiter. |
| `HIVTOOLS_MCP_CACHE_MAX_AGE` | `300` | `Cache-Control` max-age (seconds) on `/data` responses. |
| `HIVTOOLS_MCP_ENABLE_DOCS` | `true` | Serve the interactive `/docs` and `/redoc` consoles. Set `false` in production. |


## MCP

The MCP server is available at the `/mcp` endpoint. To test manually the MCP Inspector is the best way.

1. Install and run it with
   ```
   npx @modelcontextprotocol/inspector
   ```
   This will launch it in your browser.
1. Run the backend `make dev`
1. In MCP Inspector add the local server. Click "Add Servers" -> "Add manually". Use "streamable-http" as the transport and the URL to the backend url which should be "localhost:8000/mcp". Click "Add".
1. Switch the toggle to connect to the server and then click "Tools" in the top. You should see "Get Hiv Data"
1. Click on this and you can enter content in fields. They need to be valid JSON so use quotes e.g. `"MWI"` and click "Execute Tool" to see the response data.

## Deployment

Production runs on Azure Container Apps, provisioned with Terraform in [`infra/`](infra/)
(see [`infra/README.md`](infra/README.md) for setup).

To ship a code change to production:

1. Bump `version` in `pyproject.toml`.
2. Merge to `main`.
3. Cut a GitHub Release tagged `vX.Y.Z` matching that version.

Publishing the release triggers `.github/workflows/on-release-main.yml`, which builds and
pushes the image to Azure Container Registry tagged `vX.Y.Z`, rolls a new Container Apps
revision, and publishes the docs site - no manual deploy step. The release tag must match
`pyproject.toml`'s version or the deploy job fails fast.

## Development

### Setup

Setup the pre-commit hooks

```bash
make install
```

### Running the tests

```bash
make test
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the local development workflow and code-quality checks.
