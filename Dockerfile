# Install uv
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Change the working directory to the `/code` directory
WORKDIR /code

# Install dependencies from the lockfile only. This layer is cached and is
# rebuilt only when uv.lock / pyproject.toml change, not on every source edit.
COPY uv.lock pyproject.toml /code/
RUN uv sync --frozen --no-dev

# Copy the project into the image
COPY . /code

EXPOSE 80

CMD ["uv", "run", "--no-sync", "fastapi", "run", "app/main.py", "--port", "80"]
