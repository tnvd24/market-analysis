from __future__ import annotations

import os

import duckdb
import pandas as pd

from ..config import settings
from ..features.schema import FEATURE_COLUMNS
from ..news.schema import NEWS_COLUMNS
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
    -- Raw prices are stored as traded and never overwritten. adj_factor restates them for
    -- splits/bonuses (see ingest/adjust.py), so the adjustment stays reversible and auditable.
    adj_factor     DOUBLE DEFAULT 1.0,
    PRIMARY KEY (instrument_key, ts)
);
"""

CORPORATE_ACTIONS_DDL = """
CREATE TABLE IF NOT EXISTS corporate_actions (
    id           VARCHAR PRIMARY KEY,
    symbol       VARCHAR,
    isin         VARCHAR,
    ex_date      TIMESTAMP,
    action_type  VARCHAR,
    subject      VARCHAR,
    factor       DOUBLE,      -- what pre-ex-date prices are divided by; NULL = do not adjust
    needs_review BOOLEAN      -- a ratio we refused to guess at
);
"""

INSTRUMENTS_DDL = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_key VARCHAR PRIMARY KEY,
    symbol         VARCHAR,
    isin           VARCHAR,
    name           VARCHAR,
    industry       VARCHAR
);
"""

NEWS_DDL = """
CREATE TABLE IF NOT EXISTS news (
    id             VARCHAR PRIMARY KEY,
    instrument_key VARCHAR,
    symbol         VARCHAR,
    source         VARCHAR,
    published_at   TIMESTAMP,
    category       VARCHAR,
    headline       VARCHAR,
    summary        VARCHAR,
    url            VARCHAR,
    fetched_at     TIMESTAMP
);
"""

CANDLE_COLS = ["instrument_key", "symbol", "ts", "open", "high", "low", "close", "volume", "oi"]
INSTRUMENT_COLS = ["instrument_key", "symbol", "isin", "name", "industry"]
FEATURE_COLS = ["instrument_key", "symbol", "ts", *FEATURE_COLUMNS]
ACTION_COLS = [
    "id",
    "symbol",
    "isin",
    "ex_date",
    "action_type",
    "subject",
    "factor",
    "needs_review",
]

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
        self._con.execute(NEWS_DDL)
        self._con.execute(CORPORATE_ACTIONS_DDL)
        # Databases created before these columns existed.
        self._con.execute(
            "ALTER TABLE candles ADD COLUMN IF NOT EXISTS adj_factor DOUBLE DEFAULT 1.0"
        )
        self._con.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS industry VARCHAR")

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
        # Columns are named explicitly so a table can carry columns the writer doesn't set
        # (candles.adj_factor is owned by the adjustment job, not by ingestion).
        names = ", ".join(cols)
        self._con.execute(
            f"INSERT OR REPLACE INTO {table} ({names}) SELECT {names} FROM {alias}"  # noqa: S608
        )
        self._con.unregister(alias)
        self._con.commit()
        return len(staged)

    def upsert_candles(self, df: pd.DataFrame) -> int:
        return self._upsert("candles", df, CANDLE_COLS, "_c")

    def upsert_corporate_actions(self, df: pd.DataFrame) -> int:
        return self._upsert("corporate_actions", df, ACTION_COLS, "_a")

    def reset_adj_factors(self) -> None:
        """Back to 1.0 before recomputing: a stale factor distorts prices as badly as none."""
        self._con.execute("UPDATE candles SET adj_factor = 1.0")
        self._con.commit()

    def update_adj_factors(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        self._con.register("_af", df[["instrument_key", "ts", "adj_factor"]])
        self._con.execute(
            "UPDATE candles SET adj_factor = _af.adj_factor FROM _af "
            "WHERE candles.instrument_key = _af.instrument_key AND candles.ts = _af.ts"
        )
        self._con.unregister("_af")
        self._con.commit()
        return len(df)

    def upsert_instruments(self, df: pd.DataFrame) -> int:
        return self._upsert("instruments", df, INSTRUMENT_COLS, "_i")

    def upsert_features(self, df: pd.DataFrame) -> int:
        return self._upsert("features", df, FEATURE_COLS, "_f")

    def upsert_news(self, df: pd.DataFrame) -> int:
        return self._upsert("news", df, NEWS_COLUMNS, "_n")

    def latest_candle_ts(self) -> dict[str, pd.Timestamp]:
        rows = self._con.execute(
            "SELECT instrument_key, MAX(ts) AS last_ts FROM candles GROUP BY 1"
        ).fetchdf()
        return dict(zip(rows["instrument_key"], rows["last_ts"], strict=True))
