from __future__ import annotations

import os

import duckdb
import pandas as pd

from ..config import settings
from .base import StorageAdapter

CANDLES_DDL = """
CREATE TABLE IF NOT EXISTS candles (
    instrument_key VARCHAR,
    symbol         VARCHAR,
    ts             TIMESTAMP,
    open           DOUBLE,
    high           DOUBLE,
    low            DOUBLE,
    close          DOUBLE,
    volume         BIGINT,
    oi             BIGINT,
    PRIMARY KEY (instrument_key, ts)
);
"""


class DuckDBAdapter(StorageAdapter):
    def __init__(self, path: str | None = None):
        self.path = path or settings.duckdb_path
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._con = duckdb.connect(self.path)
        self._con.execute(CANDLES_DDL)

    def write_df(self, table: str, df: pd.DataFrame, mode: str = "append") -> None:
        if mode == "replace":
            self._con.execute(f"DROP TABLE IF EXISTS {table}")
        self._con.register("_df", df)
        self._con.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM _df WHERE 1=0")
        self._con.execute(f"INSERT INTO {table} SELECT * FROM _df")
        self._con.unregister("_df")

    def read_sql(self, sql: str) -> pd.DataFrame:
        return self._con.execute(sql).fetchdf()

    def upsert_candles(self, df: pd.DataFrame) -> int:
        self._con.register("_c", df)
        self._con.execute(
            "INSERT OR REPLACE INTO candles "
            "SELECT instrument_key, symbol, ts, open, high, low, close, volume, oi FROM _c"
        )
        self._con.unregister("_c")
        return len(df)
