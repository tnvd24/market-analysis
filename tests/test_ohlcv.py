from datetime import date

import pandas as pd
import pytest

from asr.ingest.ohlcv import backfill, daily_incremental, ingest_range
from asr.ingest.upstox_client import UpstoxError, parse_candles
from asr.storage.duckdb_adapter import DuckDBAdapter


class FakeClient:
    """Records the ranges it was asked for, so we can assert incremental behaviour."""

    def __init__(self, candles_by_key=None, fail: set[str] | None = None):
        self.candles_by_key = candles_by_key or {}
        self.fail = fail or set()
        self.calls: list[tuple[str, date, date]] = []

    def daily_candles(self, key, from_date, to_date):
        self.calls.append((key, from_date, to_date))
        if key in self.fail:
            raise UpstoxError(f"404 unknown instrument {key}")
        rows = [
            [f"{d.isoformat()}T00:00:00+05:30", 10.0, 11.0, 9.0, 10.5, 1000, 0]
            for d in self.candles_by_key.get(key, [])
            if from_date <= d <= to_date
        ]
        return parse_candles(rows, key)


@pytest.fixture
def storage(tmp_path):
    db = DuckDBAdapter(path=str(tmp_path / "t.duckdb"))
    db.upsert_instruments(
        pd.DataFrame(
            [
                {"instrument_key": "NSE_EQ|A", "symbol": "AAA", "isin": "INE_A", "name": "A Ltd"},
                {"instrument_key": "NSE_EQ|B", "symbol": "BBB", "isin": "INE_B", "name": "B Ltd"},
            ]
        )
    )
    return db


def test_ingest_stores_candles_with_the_universe_symbol(storage):
    client = FakeClient({"NSE_EQ|A": [date(2026, 1, 1), date(2026, 1, 2)]})

    report = ingest_range(
        ["NSE_EQ|A"], date(2026, 1, 1), date(2026, 1, 31), client=client, storage=storage
    )

    assert report.rows == 2
    assert report.instruments == 1
    out = storage.read_sql("SELECT symbol, close FROM candles")
    assert out["symbol"].unique().tolist() == ["AAA"]  # joined from instruments, not the key


def test_rerunning_a_backfill_does_not_duplicate_rows(storage):
    client = FakeClient({"NSE_EQ|A": [date(2026, 1, 1), date(2026, 1, 2)]})

    backfill(["NSE_EQ|A"], years=1, to_date=date(2026, 1, 31), client=client, storage=storage)
    backfill(["NSE_EQ|A"], years=1, to_date=date(2026, 1, 31), client=client, storage=storage)

    assert storage.read_sql("SELECT COUNT(*) AS n FROM candles").iloc[0]["n"] == 2


def test_daily_incremental_only_asks_for_what_is_missing(storage):
    client = FakeClient({"NSE_EQ|A": [date(2026, 1, 5), date(2026, 1, 6)]})
    storage.upsert_candles(
        pd.DataFrame(
            [
                {
                    "instrument_key": "NSE_EQ|A",
                    "symbol": "AAA",
                    "ts": pd.Timestamp("2026-01-05"),
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1,
                    "oi": 0,
                }
            ]
        )
    )

    daily_incremental(["NSE_EQ|A"], to_date=date(2026, 1, 6), client=client, storage=storage)

    key, from_date, to_date = client.calls[0]
    assert from_date == date(2026, 1, 5)  # re-fetches the last stored day, nothing earlier
    assert to_date == date(2026, 1, 6)
    # the overlapping day is replaced, the new day appended
    assert storage.read_sql("SELECT COUNT(*) AS n FROM candles").iloc[0]["n"] == 2


def test_cold_start_instrument_gets_a_long_lookback(storage):
    client = FakeClient({})
    daily_incremental(["NSE_EQ|B"], to_date=date(2026, 1, 6), client=client, storage=storage)

    _, from_date, _ = client.calls[0]
    assert (date(2026, 1, 6) - from_date).days > 300


def test_one_bad_instrument_does_not_abort_the_run(storage):
    client = FakeClient({"NSE_EQ|B": [date(2026, 1, 2)]}, fail={"NSE_EQ|A"})

    report = ingest_range(
        ["NSE_EQ|A", "NSE_EQ|B"],
        date(2026, 1, 1),
        date(2026, 1, 31),
        client=client,
        storage=storage,
    )

    assert "NSE_EQ|A" in report.failures
    assert report.rows == 1  # B still ingested
