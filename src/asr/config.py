"""Settings, read from .env.

**Nothing here is required to run the system.** Prices, corporate actions and filings all
come from NSE, which needs no credentials — the defaults below are enough for a full
ingest → indicators → research pack run on a fresh clone.

The one optional credential is an Upstox token, and only to add their *news* feed on top of
NSE's filings. There is deliberately no Anthropic key: the qualitative read happens by
pasting a research pack into Claude on a subscription, so no model ever touches the data.
See docs/decisions.md.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Optional: only unlocks the Upstox news feed. Prices come from NSE, without auth.
    upstox_access_token: str | None = None  # read-only Analytics Token

    # Storage: DuckDB locally (free), BigQuery in prod. Same StorageAdapter interface.
    storage_backend: str = "duckdb"  # "duckdb" | "bigquery"
    duckdb_path: str = "./data/asr.duckdb"
    gcp_project: str | None = None
    bq_dataset: str = "asr"


# TODO(phase9): prod secrets. Local dev reads .env (above). In prod, load the optional token
# from GCP Secret Manager here, so no secret ever touches the image or an env file.
settings = Settings()
