from datetime import datetime

import pandas as pd

from asr.storage.duckdb_adapter import DuckDBAdapter


def test_upsert_is_idempotent(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    row = dict(
        instrument_key="NSE_EQ|X",
        symbol="X",
        ts=datetime(2026, 1, 1),
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=100,
        oi=0,
    )
    df = pd.DataFrame([row])
    db.upsert_candles(df)
    db.upsert_candles(df)  # second write must not duplicate
    out = db.read_sql("SELECT COUNT(*) AS n FROM candles")
    assert out.iloc[0]["n"] == 1
