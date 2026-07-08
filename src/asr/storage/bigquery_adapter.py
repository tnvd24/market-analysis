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
