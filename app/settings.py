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


settings = Settings()
