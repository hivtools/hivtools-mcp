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
