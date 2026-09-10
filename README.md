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

### Running the API

For local development (reloads on file changes):

```bash
make dev
# equivalent to: uv run fastapi dev app/main.py
```

This serves the API at <http://127.0.0.1:8000>, with interactive docs at <http://127.0.0.1:8000/docs>.

To run it as it runs in production:

```bash
uv run fastapi run app/main.py
```

### With Docker

```bash
docker build -t hivtools-mcp .
docker run -p 80:80 hivtools-mcp
```

The API is then served at <http://127.0.0.1:80>.

## Data preparation

The API is served from a Parquet dataset built from Naomi model output zips.
[`data-prep/extract_indicators.py`](data-prep/extract_indicators.py) takes a
directory of zips, reads `indicators.csv` out of each one (no need to unzip
first), keeps the columns the API needs, and writes a Hive-partitioned dataset
keyed by `country`.

```bash
uv run --script data-prep/extract_indicators.py data-prep/raw-data
```

Every `*.zip` in the directory is processed. Each country's ISO3 code is read
from the national (`area_level 0`) row in its data. The result is:

```
data-prep/naomi-data/
  country=MWI/00000000.parquet
  country=ZWE/00000000.parquet
```

Query it with partition pruning, e.g.
`pl.scan_parquet("data-prep/naomi-data/").filter(pl.col("country") == "MWI")`, or
point DuckDB at `data-prep/naomi-data/`. Re-running replaces each country's
partition, so it is safe to repeat. Change the output root with `--out-dir`; see
`--help` for details.

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
