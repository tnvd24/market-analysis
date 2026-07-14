from __future__ import annotations

import pandas as pd

from ..config import settings
from .base import StorageAdapter


class BigQueryAdapter(StorageAdapter):
    """Prod backend. Same interface as DuckDB so calling code never changes. (Phase 8-9)"""

    def __init__(self):
        from google.cloud import bigquery

        self.client = bigquery.Client(project=settings.gcp_project)
        self.dataset = settings.bq_dataset

    def _tbl(self, table: str) -> str:
        return f"{settings.gcp_project}.{self.dataset}.{table}"

    def write_df(self, table: str, df: pd.DataFrame, mode: str = "append") -> None:
        from google.cloud import bigquery

        disp = "WRITE_TRUNCATE" if mode == "replace" else "WRITE_APPEND"
        job = self.client.load_table_from_dataframe(
            df,
            self._tbl(table),
            job_config=bigquery.LoadJobConfig(write_disposition=disp),
        )
        job.result()

    def read_sql(self, sql: str) -> pd.DataFrame:
        return self.client.query(sql).to_dataframe()

    def upsert_candles(self, df: pd.DataFrame) -> int:
        # TODO(phase8): MERGE via staging table for true idempotency.
        self.write_df("candles", df, mode="append")
        return len(df)

    def upsert_features(self, df: pd.DataFrame) -> int:
        # TODO(phase8): MERGE via staging table, same as candles.
        self.write_df("features", df, mode="append")
        return len(df)

    def upsert_corporate_actions(self, df: pd.DataFrame) -> int:
        self.write_df("corporate_actions", df, mode="replace")
        return len(df)

    def reset_adj_factors(self) -> None:
        self.client.query(
            f"UPDATE `{self._tbl('candles')}` SET adj_factor = 1.0 WHERE TRUE"
        ).result()

    def update_adj_factors(self, df: pd.DataFrame) -> int:
        # TODO(phase8): MERGE from a staging table, same shape as the candle upsert.
        raise NotImplementedError("adj_factor updates need a staging MERGE on BigQuery")

    def upsert_news(self, df: pd.DataFrame) -> int:
        # TODO(phase8): MERGE on id. News windows always overlap, so append-only would
        # duplicate heavily here — this must land before the BigQuery backend is used.
        self.write_df("news", df, mode="append")
        return len(df)

    def upsert_instruments(self, df: pd.DataFrame) -> int:
        # The universe is small and fully re-derived each sync, so truncate-and-load
        # is both idempotent and cheap here.
        self.write_df("instruments", df, mode="replace")
        return len(df)

    def latest_candle_ts(self) -> dict[str, pd.Timestamp]:
        rows = self.read_sql(
            f"SELECT instrument_key, MAX(ts) AS last_ts FROM `{self._tbl('candles')}` GROUP BY 1"  # noqa: S608
        )
        return dict(zip(rows["instrument_key"], rows["last_ts"], strict=True))
