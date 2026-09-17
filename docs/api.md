# API reference

Every route below is also an MCP tool, except the `health` and `meta` ones: the
MCP server at `/mcp` is generated from this API's OpenAPI schema. `/search` is the
`search_hiv_metadata` tool and `/data` is `get_hiv_data`, and their docstrings are
the tool descriptions a model reads.

Full schema and a try-it console are at `/docs` when the API is running (disabled in
production with `HIVTOOLS_MCP_ENABLE_DOCS=false`; `/openapi.json` stays available).

## `GET /`, `GET /version`

Both return `{"name": "hivtools-mcp", "version": "<pyproject version>"}`.

## `GET /health`, `GET /health/ready`

`/health` is a liveness check (`{"status": "ok"}`). `/health/ready` also checks the DuckDB
connection answers a query (`{"status": "ready"}`, or `503`). Used by the Azure Container
Apps probes.

## `GET /search`

MCP tool: `search_hiv_metadata`.

Resolves plain-language terms to the IDs `/data` accepts. Nothing in this dataset
has a guessable ID - the treatment gap is `untreated_plhiv_num`, Lilongwe is
`MWI_3_13_demo`, children are `Y000_014` - and guessing returns an empty result
rather than an error, so terms are resolved here first.

One index covers indicators, areas, age groups, age partitions, risk groups and
concepts, each result tagged with its `field`. Repeat `q` to resolve several terms
in one call:

```bash
curl "http://127.0.0.1:8000/search?q=treatment+gap&q=children&q=Lilongwe&country=MWI"
```

| Parameter | Purpose |
| --- | --- |
| `q` | Term to resolve, as a user would phrase it. Repeat for several. |
| `field` | Restrict to `concept`, `indicator`, `area`, `age_group`, `age_partition` or `risk_group`. Omit when unsure. |
| `country` | ISO3 code; scopes areas, age groups and coverage to one country. |
| `limit` | Maximum matches per term (default 5). |

The best match comes back in full (`detail: "full"`), the rest as summaries. For a
concept that means the indicators it maps to, their units, the dimension values
they actually have rows for, and the caveats that make an answer correct - all of
which the caller needs *before* querying, not after. An indicator's `coverage` is
keyed by source, since Naomi and Spectrum cover very different ground; a risk
group says which sources and sexes have it.

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

## `GET /data`

MCP tool: `get_hiv_data`.

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
estimates, whose label says how the figures must be described (or `sources`, one
per source, when the rows come from several). A dimension is only hoisted when it
is provably constant across the whole result, not merely constant on the page in
hand.

### Filters

To match any of several values (`OR`), repeat the parameter
(`?indicator=a&indicator=b`) or comma-separate it (`?indicator=a,b`); different
parameters combine with `AND`:

| Parameter | Notes |
| --- | --- |
| `country` | ISO3, e.g. `MWI` - the partition key, so this is the cheapest filter |
| `source` | `naomi`, `spectrum` or `shipp`. An indicator from two sources comes back as two rows: pick one, never add them |
| `area_level` | integer, `0`-`4` |
| `area_id` | e.g. `MWI`, `MWI_1_1_demo` |
| `sex` | `both`, `female`, `male` |
| `age_group` | e.g. `Y015_049` |
| `risk_group` | e.g. `sexpaid12m`, `msm`; `all` for Naomi and Spectrum rows |
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

### Shape

| Parameter | Default | Notes |
| --- | --- | --- |
| `columns` | all six | Which estimate columns to return: `mean`, `se`, `median`, `mode`, `lower`, `upper`. Repeat or comma-separate. This is a projection, not a row filter - the categorical columns above are always returned. Spectrum and SHIPP only have `mean`; their other measures are null. |
| `limit` | `1000` | 1-5000 (`HIVTOOLS_MCP_MAX_ROWS`) |
| `offset` | `0` | Results are ordered by the categorical columns, so `limit`/`offset` paginate deterministically. |
| `sig_figs` | `6` (`HIVTOOLS_MCP_RESPONSE_SIG_FIGS`) | Significant figures the measure values are rounded to on the way out, 1-15. The Parquet dataset keeps full model precision; this is presentation only. |

An unknown `columns` value, a non-integer `area_level`, or a `limit`/`sig_figs` outside its
range, is a `422`. `/data` is rate limited per client IP (`HIVTOOLS_MCP_DATA_RATE_LIMIT`,
default `300/minute`); over that is a `429`. Responses carry `Cache-Control: public,
max-age=300` (`HIVTOOLS_MCP_CACHE_MAX_AGE`).

### Examples

```bash
# HIV prevalence for 15-49s, national level, just the point estimate and CI
curl "http://127.0.0.1:8000/data?indicator=prevalence&area_id=MWI&age_group=Y015_049&columns=mean,lower,upper"

# ART coverage for two countries, women, most recent quarter
curl "http://127.0.0.1:8000/data?indicator=art_coverage&country=MWI,ZWE&sex=female&calendar_quarter=CY2024Q3"

# National PLHIV for every year, from Spectrum
curl "http://127.0.0.1:8000/data?source=spectrum&indicator=plhiv&country=TZA&sex=both&age_group=Y000_999&columns=mean"

# Female sex workers by district, from SHIPP (with auth on)
curl -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000/data?source=shipp&risk_group=sexpaid12m&indicator=population&country=TZA&area_level=4&sex=female&age_group=Y015_049"
```

## Authentication

When `HIVTOOLS_MCP_API_TOKEN` is set, every request needs
`Authorization: Bearer <token>` - `/data`, `/search` and `/mcp` alike - and gets a
`401` without it. `/`, `/version`, `/health`, `/health/ready` and the OpenAPI
schema and docs stay open. Locally the token is unset, so the API is open.

In production the token comes from Terraform
(`terraform output -raw api_token`), and the claude.ai connector sends it as a
static request header, set by an organisation admin when adding the connector.
Rotating it is covered in
[`infra/README.md`](https://github.com/hivtools/hivtools-mcp/blob/main/infra/README.md#authentication).

The container image sets `HIVTOOLS_MCP_REQUIRE_AUTH=true`, so it will not start
without a token.

## Configuration

Environment variables (or a `.env` file), all prefixed `HIVTOOLS_MCP_`. They are
defined in
[`app/settings.py`](https://github.com/hivtools/hivtools-mcp/blob/main/app/settings.py).

| Variable | Default | Purpose |
| --- | --- | --- |
| `HIVTOOLS_MCP_NAOMI_DATA_DIR` | `data-prep/naomi-data` | Root of the Parquet dataset to serve. |
| `HIVTOOLS_MCP_API_TOKEN` | unset (open) | Bearer token required on every data request. See [Authentication](#authentication). |
| `HIVTOOLS_MCP_REQUIRE_AUTH` | `false` (`true` in the image) | Refuse to start without `HIVTOOLS_MCP_API_TOKEN`. |
| `HIVTOOLS_MCP_SEARCH_RATE_LIMIT` | `600/minute` | Per-IP limit on `/search`; looser than `/data` since an agent makes several lookups per question. |
| `HIVTOOLS_MCP_DUCKDB_THREADS` | unset (one per core) | Caps threads per query; lower it if many concurrent requests oversubscribe the CPU. |
| `HIVTOOLS_MCP_RESPONSE_SIG_FIGS` | `6` | Default significant figures for measure values in responses; a request can override with `?sig_figs=`. |
| `HIVTOOLS_MCP_MAX_ROWS` | `5000` | Largest page `?limit=` may request. |
| `HIVTOOLS_MCP_DEFAULT_ROWS` | `1000` | Page size when `?limit=` is omitted. |
| `HIVTOOLS_MCP_DATA_RATE_LIMIT` | `300/minute` | Per-IP rate limit on `/data`. Every claude.ai user arrives from a few Anthropic addresses, so this is shared between them. |
| `HIVTOOLS_MCP_RATE_LIMIT_ENABLED` | `true` | Master switch for the rate limiter. |
| `HIVTOOLS_MCP_CACHE_MAX_AGE` | `300` | `Cache-Control` max-age (seconds) on `/data` responses. |
| `HIVTOOLS_MCP_ENABLE_DOCS` | `true` | Serve the interactive `/docs` and `/redoc` consoles. Set `false` in production. |
| `HIVTOOLS_MCP_LOG_JSON` | `false` | Log JSON lines to stdout (for Log Analytics) instead of readable text to stderr. On in production. |
| `HIVTOOLS_MCP_LOG_LEVEL` | `INFO` | Minimum log level. |
| `HIVTOOLS_MCP_LOG_RESPONSE_CHARS` | `2000` | How much of each MCP tool response goes into its log line. |
