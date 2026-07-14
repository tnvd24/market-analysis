from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class StorageAdapter(ABC):
    """One interface, two backends. DuckDB locally, BigQuery in prod."""

    @abstractmethod
    def write_df(self, table: str, df: pd.DataFrame, mode: str = "append") -> None: ...

    @abstractmethod
    def read_sql(self, sql: str) -> pd.DataFrame: ...

    @abstractmethod
    def upsert_candles(self, df: pd.DataFrame) -> int:
        """Idempotent write of OHLCV keyed by (instrument_key, ts)."""
        ...

    @abstractmethod
    def upsert_instruments(self, df: pd.DataFrame) -> int:
        """Idempotent write of the resolved universe, keyed by instrument_key."""
        ...

    @abstractmethod
    def latest_candle_ts(self) -> dict[str, pd.Timestamp]:
        """instrument_key -> newest stored candle ts. Drives incremental ingest."""
        ...

    @abstractmethod
    def upsert_features(self, df: pd.DataFrame) -> int:
        """Idempotent write of indicators keyed by (instrument_key, ts)."""
        ...


def get_storage() -> StorageAdapter:
    from ..config import settings

    if settings.storage_backend == "bigquery":
        from .bigquery_adapter import BigQueryAdapter

        return BigQueryAdapter()
    from .duckdb_adapter import DuckDBAdapter

    return DuckDBAdapter()
