from __future__ import annotations

import os

import duckdb
import pandas as pd

from ..config import settings
from ..features.schema import FEATURE_COLUMNS
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

INSTRUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_key VARCHAR PRIMARY KEY,
    symbol         VARCHAR,
    isin           VARCHAR,
    name           VARCHAR
);
"""

CANDLE_COLS = ["instrument_key", "symbol", "ts", "open", "high", "low", "close", "volume", "oi"]
INSTRUMENT_COLS = ["instrument_key", "symbol", "isin", "name"]
FEATURE_COLS = ["instrument_key", "symbol", "ts", *FEATURE_COLUMNS]

# Derived from FEATURE_COLUMNS so the table can never drift from what the layer computes.
FEATURES_DDL = f"""
CREATE TABLE IF NOT EXISTS features (
    instrument_key VARCHAR,
    symbol         VARCHAR,
    ts             TIMESTAMP,
    {", ".join(f"{c} DOUBLE" for c in FEATURE_COLUMNS)},
    PRIMARY KEY (instrument_key, ts)
);
"""


class DuckDBAdapter(StorageAdapter):
    def __init__(self, path: str | None = None):
        self.path = path or settings.duckdb_path
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._con = duckdb.connect(self.path)
        self._con.execute(CANDLES_DDL)
        self._con.execute(INSTRUMENTS_DDL)
        self._con.execute(FEATURES_DDL)

    def write_df(self, table: str, df: pd.DataFrame, mode: str = "append") -> None:
        if mode == "replace":
            self._con.execute(f"DROP TABLE IF EXISTS {table}")
        self._con.register("_df", df)
        self._con.execute(f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM _df WHERE 1=0")
        self._con.execute(f"INSERT INTO {table} SELECT * FROM _df")
        self._con.unregister("_df")
        self._con.commit()

    def read_sql(self, sql: str) -> pd.DataFrame:
        return self._con.execute(sql).fetchdf()

    def _upsert(self, table: str, df: pd.DataFrame, cols: list[str], alias: str) -> int:
        if df.empty:
            return 0
        staged = df.reindex(columns=cols)  # missing columns arrive as NULL, extras dropped
        self._con.register(alias, staged)
        self._con.execute(
            f"INSERT OR REPLACE INTO {table} SELECT {', '.join(cols)} FROM {alias}"  # noqa: S608
        )
        self._con.unregister(alias)
        self._con.commit()
        return len(staged)

    def upsert_candles(self, df: pd.DataFrame) -> int:
        return self._upsert("candles", df, CANDLE_COLS, "_c")

    def upsert_instruments(self, df: pd.DataFrame) -> int:
        return self._upsert("instruments", df, INSTRUMENT_COLS, "_i")

    def upsert_features(self, df: pd.DataFrame) -> int:
        return self._upsert("features", df, FEATURE_COLS, "_f")

    def latest_candle_ts(self) -> dict[str, pd.Timestamp]:
        rows = self._con.execute(
            "SELECT instrument_key, MAX(ts) AS last_ts FROM candles GROUP BY 1"
        ).fetchdf()
        return dict(zip(rows["instrument_key"], rows["last_ts"], strict=True))
