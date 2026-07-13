from datetime import datetime

import pandas as pd
import pytest

from asr.storage.duckdb_adapter import DuckDBAdapter


def _candle(**over):
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
    row.update(over)
    return row


@pytest.fixture
def db(tmp_path):
    return DuckDBAdapter(path=str(tmp_path / "t.duckdb"))


def test_upsert_is_idempotent(db):
    df = pd.DataFrame([_candle()])
    db.upsert_candles(df)
    db.upsert_candles(df)  # second write must not duplicate
    assert db.read_sql("SELECT COUNT(*) AS n FROM candles").iloc[0]["n"] == 1


def test_upsert_replaces_a_revised_candle(db):
    db.upsert_candles(pd.DataFrame([_candle(close=1.5)]))
    db.upsert_candles(pd.DataFrame([_candle(close=9.9)]))  # same (key, ts), new close

    out = db.read_sql("SELECT close FROM candles")
    assert len(out) == 1
    assert out.iloc[0]["close"] == 9.9


def test_upsert_tolerates_a_frame_without_symbol(db):
    """The client returns candles without a symbol; the ingestor may not have one."""
    df = pd.DataFrame([_candle()]).drop(columns=["symbol"])
    assert db.upsert_candles(df) == 1
    assert db.read_sql("SELECT symbol FROM candles").iloc[0]["symbol"] is None


def test_instruments_upsert_is_keyed_by_instrument_key(db):
    df = pd.DataFrame(
        [{"instrument_key": "NSE_EQ|X", "symbol": "X", "isin": "INE_X", "name": "Old Name"}]
    )
    db.upsert_instruments(df)
    db.upsert_instruments(df.assign(name="New Name"))

    out = db.read_sql("SELECT name FROM instruments")
    assert len(out) == 1
    assert out.iloc[0]["name"] == "New Name"


def test_latest_candle_ts_drives_incremental_ingest(db):
    db.upsert_candles(
        pd.DataFrame(
            [
                _candle(ts=datetime(2026, 1, 1)),
                _candle(ts=datetime(2026, 1, 5)),
                _candle(instrument_key="NSE_EQ|Y", symbol="Y", ts=datetime(2026, 1, 3)),
            ]
        )
    )

    latest = db.latest_candle_ts()
    assert latest["NSE_EQ|X"] == pd.Timestamp("2026-01-05")
    assert latest["NSE_EQ|Y"] == pd.Timestamp("2026-01-03")


def test_latest_candle_ts_is_empty_on_a_fresh_db(db):
    assert db.latest_candle_ts() == {}
