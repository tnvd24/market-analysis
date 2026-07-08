from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Upstox (research-only)
    upstox_access_token: str | None = None  # Analytics Token (preferred)
    upstox_api_key: str | None = None
    upstox_api_secret: str | None = None
    upstox_redirect_uri: str = "http://localhost:8080/callback"

    # Anthropic (Phase 4-5)
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"

    # Storage
    storage_backend: str = "duckdb"  # "duckdb" | "bigquery"
    duckdb_path: str = "./data/asr.duckdb"
    gcp_project: str | None = None
    bq_dataset: str = "asr"


# TODO(phase9): prod secrets. Local dev reads .env (above). In prod, load secrets
# from GCP Secret Manager here (e.g. a customise_sources / model_validator hook that
# fills the *_api_key / *_token fields from Secret Manager when running on GCP) so no
# secret ever touches the image or env files. See infra/ for the deploy wiring.
settings = Settings()
