import numpy as np
import pandas as pd
import pytest

from asr.features.build import build_features, latest_features
from asr.storage.duckdb_adapter import DuckDBAdapter
from tests.test_indicators import make_candles


@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    rng = np.random.default_rng(4)
    for key, symbol in [("NSE_EQ|A", "AAA"), ("NSE_EQ|B", "BBB")]:
        closes = list(100 + np.cumsum(rng.normal(size=60)))
        candles = make_candles(closes, key=key, symbol=symbol)
        db.upsert_candles(candles.assign(oi=0))
    return db


def test_build_writes_one_feature_row_per_candle(storage):
    report = build_features(storage=storage)

    assert report.instruments == 2
    assert report.rows == 120
    stored = storage.read_sql("SELECT COUNT(*) AS n FROM features").iloc[0]["n"]
    assert stored == 120


def test_rebuild_refreshes_rather_than_duplicates(storage):
    build_features(storage=storage)
    build_features(storage=storage)  # e.g. after new candles land

    assert storage.read_sql("SELECT COUNT(*) AS n FROM features").iloc[0]["n"] == 120


def test_warmup_rows_are_stored_null_not_zero(storage):
    build_features(storage=storage)

    early = storage.read_sql(
        "SELECT sma_50 FROM features WHERE instrument_key = 'NSE_EQ|A' ORDER BY ts LIMIT 5"
    )
    assert early["sma_50"].isna().all()


def test_thin_history_is_reported_not_silently_wrong(storage):
    """60 bars < the 200 an SMA-200 needs — the run must say so."""
    report = build_features(storage=storage)
    assert set(report.thin) == {"NSE_EQ|A", "NSE_EQ|B"}


def test_build_can_target_a_subset(storage):
    report = build_features(["NSE_EQ|A"], storage=storage)

    assert report.instruments == 1
    keys = storage.read_sql("SELECT DISTINCT instrument_key FROM features")
    assert keys["instrument_key"].tolist() == ["NSE_EQ|A"]


def test_latest_features_returns_the_newest_row_per_instrument(storage):
    build_features(storage=storage)

    latest = latest_features(storage=storage)

    assert len(latest) == 2
    assert latest["symbol"].tolist() == ["AAA", "BBB"]
    newest_a = storage.read_sql(
        "SELECT MAX(ts) AS ts FROM features WHERE instrument_key = 'NSE_EQ|A'"
    ).iloc[0]["ts"]
    assert latest[latest["symbol"] == "AAA"].iloc[0]["ts"] == pd.Timestamp(newest_a)
