# syntax=docker/dockerfile:1

# Stage 1: build the demo Parquet dataset from the committed Naomi output zips.
# This runs on every image build, so to ship new demo data you just refresh
# data-prep/raw-data/ and rebuild - there is no separate extract step to remember,
# locally or in CI.
FROM python:3.14-slim AS demo-data
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /build
COPY data-prep ./data-prep
RUN uv run --script data-prep/extract_indicators.py data-prep/raw-data --out-dir /demo-data

# Stage 2: the runtime image.
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /code

# Install dependencies from the lockfile only. This layer is cached and is
# rebuilt only when uv.lock / pyproject.toml change, not on every source edit.
COPY uv.lock pyproject.toml /code/
RUN uv sync --frozen --no-dev

# Application code, then the demo dataset built in stage 1.
COPY app /code/app
COPY --from=demo-data /demo-data /code/demo-data
ENV HIVTOOLS_MCP_NAOMI_DATA_DIR=/code/demo-data

EXPOSE 80

CMD ["uv", "run", "--no-sync", "fastapi", "run", "app/main.py", "--port", "80"]
