from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, overridable via ``HIVTOOLS_MCP_*`` env vars or a .env file."""

    model_config = SettingsConfigDict(env_prefix="HIVTOOLS_MCP_", env_file=".env", extra="ignore")

    # Root of the Hive-partitioned Parquet dataset produced by data-prep/extract_indicators.py.
    naomi_data_dir: Path = Path("data-prep/naomi-data")

    # Per-query DuckDB thread cap. None leaves DuckDB's default (one thread per core);
    # set a small number to avoid oversubscribing cores under concurrent API load.
    duckdb_threads: int | None = None

    # Significant figures the /data endpoint rounds measure values to. The Parquet
    # dataset keeps full model precision; this is presentation only, and a request
    # can override it with ?sig_figs=.
    response_sig_figs: int = 6

    # /data pagination bounds. max_rows caps the largest page a caller can pull in
    # one request - the main lever on response size, and so on egress cost.
    max_rows: int = 5_000
    default_rows: int = 1_000

    # Per-IP rate limit for /data (slowapi syntax, e.g. "30/minute"). Disable it
    # wholesale with rate_limit_enabled=False (the test suite does).
    data_rate_limit: str = "30/minute"
    rate_limit_enabled: bool = True

    # Cache-Control max-age (seconds) sent with /data responses. The dataset only
    # changes on a redeploy, so responses are safely cacheable by any shared proxy.
    cache_max_age: int = 300

    # Serve the interactive /docs and /redoc consoles. Turned off in production -
    # they are an abuse magnet and add nothing for machine callers. /openapi.json
    # is unaffected.
    enable_docs: bool = True


settings = Settings()
