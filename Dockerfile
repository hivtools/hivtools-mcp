# syntax=docker/dockerfile:1

# Stage 1: the dataset the image serves.
#
# By default this builds the public demo data committed in data-prep/raw-data,
# which needs only Python, so a plain `docker build` always works. Releases build
# the full dataset - private inputs included, whose Spectrum and SHIPP files need
# R - outside Docker, and hand it in as a named build context, which replaces the
# `data` stage below:
#
#   docker build --build-context data=data-prep/naomi-data .    (or `make docker`)
#
# That way the private inputs never enter the main build context, and only the
# built Parquet ends up in the image.
FROM python:3.14-slim AS demo-data
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /build
COPY data-prep ./data-prep
RUN uv run --script data-prep/extract_indicators.py data-prep/raw-data/datasets.yaml --out-dir /data

FROM scratch AS data
COPY --from=demo-data /data /

# Stage 2: the runtime image.
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /code

# Install dependencies from the lockfile only. This layer is cached and is
# rebuilt only when uv.lock / pyproject.toml change, not on every source edit.
COPY uv.lock pyproject.toml /code/
RUN uv sync --frozen --no-dev

# Application code, then the dataset from stage 1.
COPY app /code/app
COPY --from=data / /code/data
ENV HIVTOOLS_MCP_NAOMI_DATA_DIR=/code/data

# The dataset may not be public, so the image refuses to start without an API
# token (HIVTOOLS_MCP_API_TOKEN) rather than serving it to anyone.
ENV HIVTOOLS_MCP_REQUIRE_AUTH=true

EXPOSE 80

CMD ["uv", "run", "--no-sync", "fastapi", "run", "app/main.py", "--port", "80"]
