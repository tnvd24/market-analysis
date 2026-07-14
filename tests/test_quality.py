"""Each test here stages a failure that would otherwise run clean.

That is the whole point of the layer: none of these states raise an exception on their own.
They just quietly produce a confident, wrong number.
"""

from datetime import date, timedelta

import pandas as pd
import pytest

from asr.quality.checks import (
    ERROR,
    WARN,
    check_duplicate_candles,
    check_feature_coverage,
    check_future_timestamps,
    check_gaps,
    check_ohlc_sanity,
    check_price_jumps,
    check_staleness,
)
from asr.storage.duckdb_adapter import DuckDBAdapter


def candle(
    ts, close=100.0, open_=None, high=None, low=None, volume=1000, key="NSE_EQ|A", sym="AAA"
):
    open_ = close if open_ is None else open_
    return {
        "instrument_key": key,
        "symbol": sym,
        "ts": pd.Timestamp(ts),
        "open": open_,
        "high": max(open_, close) + 1 if high is None else high,
        "low": min(open_, close) - 1 if low is None else low,
        "close": close,
        "volume": volume,
        "oi": 0,
    }


@pytest.fixture
def db(tmp_path):
    return DuckDBAdapter(path=str(tmp_path / "t.duckdb"))


def _store(db, rows):
    db.upsert_candles(pd.DataFrame(rows))
    return db


# --- the split that isn't adjusted (the open question this check answers) -----


def test_an_unadjusted_split_is_caught_as_a_price_jump(db):
    """A 1:2 split halves the price overnight. Nothing errors — RSI just goes insane."""
    _store(
        db,
        [
            candle("2026-01-01", close=1000.0),
            candle("2026-01-02", close=500.0),  # the "crash" that is really a split
        ],
    )

    findings = check_price_jumps(db)

    assert len(findings) == 1
    assert findings[0].severity == WARN
    assert findings[0].symbol == "AAA"
    assert "-50.0%" in findings[0].detail
    assert "split" in findings[0].detail  # the hint fires near a clean split ratio


def test_ordinary_volatility_does_not_trip_the_jump_check(db):
    _store(db, [candle("2026-01-01", close=100.0), candle("2026-01-02", close=105.0)])
    assert check_price_jumps(db) == []


# --- candles that are not candles ---------------------------------------------


def test_impossible_ohlc_is_an_error(db):
    """high < low is not a price — it's a corrupt row that will still compute an ATR."""
    _store(db, [candle("2026-01-01", close=100.0, high=90.0, low=110.0)])

    findings = check_ohlc_sanity(db)

    assert findings[0].severity == ERROR
    assert findings[0].symbol == "AAA"


def test_a_close_outside_the_high_low_range_is_an_error(db):
    _store(db, [candle("2026-01-01", close=150.0, open_=100.0, high=120.0, low=90.0)])
    assert check_ohlc_sanity(db)[0].severity == ERROR


def test_clean_candles_produce_no_findings(db):
    _store(db, [candle("2026-01-01"), candle("2026-01-02")])
    assert check_ohlc_sanity(db) == []


# --- holes, staleness, timezone bugs ------------------------------------------


def test_a_long_hole_in_the_series_is_flagged(db):
    """A gap shifts every rolling window that spans it — silently."""
    _store(db, [candle("2026-01-01"), candle("2026-02-01")])  # a month missing

    findings = check_gaps(db)

    assert len(findings) == 1
    assert "31-day hole" in findings[0].detail


def test_a_weekend_is_not_a_hole(db):
    _store(db, [candle("2026-01-02"), candle("2026-01-05")])  # Fri -> Mon
    assert check_gaps(db) == []


def test_stale_data_is_reported_against_a_fixed_as_of(db):
    _store(db, [candle("2026-01-01")])

    fresh = check_staleness(db, as_of=date(2026, 1, 3))
    stale = check_staleness(db, as_of=date(2026, 1, 20))

    assert fresh == []
    assert stale[0].severity == ERROR  # 19 days > 3x the 5-day tolerance
    assert "19 days old" in stale[0].detail


def test_an_empty_warehouse_is_an_error_not_a_pass(db):
    findings = check_staleness(db, as_of=date(2026, 1, 1))
    assert findings[0].severity == ERROR
    assert "no candles" in findings[0].detail


def test_a_future_dated_candle_is_an_error(db):
    """The signature of a UTC/IST mix-up: an evening bar lands on tomorrow."""
    tomorrow = date.today() + timedelta(days=2)
    _store(db, [candle(tomorrow.isoformat())])

    findings = check_future_timestamps(db)

    assert findings[0].severity == ERROR
    assert "timezone" in findings[0].detail


def test_todays_candle_is_not_treated_as_the_future(db):
    _store(db, [candle(date.today().isoformat())])
    assert check_future_timestamps(db) == []


# --- coverage -----------------------------------------------------------------


def test_candles_without_features_are_reported(db):
    """Indicators silently out of date is the same class of bug: no error, stale answers."""
    _store(db, [candle("2026-01-01"), candle("2026-01-02")])

    findings = check_feature_coverage(db)

    assert findings[0].severity == WARN
    assert "2 candles have no features" in findings[0].detail


def test_duplicate_candles_would_be_an_error(db):
    _store(db, [candle("2026-01-01")])
    assert check_duplicate_candles(db) == []  # the primary key holds


# --- the report ---------------------------------------------------------------


UNIVERSE = pd.DataFrame([{"symbol": "AAA", "isin": "INE_A", "name": "A Ltd"}])


def test_report_is_not_ok_when_an_error_is_present(db):
    from asr.quality.checks import QualityReport, run_checks

    _store(db, [candle("2026-01-01", close=100.0, high=90.0, low=110.0)])
    report = run_checks(db, as_of=date(2026, 1, 2), universe=UNIVERSE)

    assert isinstance(report, QualityReport)
    assert not report.ok
    assert any(f.check == "ohlc_sanity" for f in report.errors)


def test_a_clean_warehouse_reports_ok(db):
    from asr.quality.checks import run_checks

    rows = [candle(f"2026-01-{d:02d}", close=100.0 + d) for d in range(1, 6)]
    _store(db, rows)
    db.upsert_instruments(
        pd.DataFrame([{"instrument_key": "NSE_EQ|A", "symbol": "AAA", "isin": "I", "name": "A"}])
    )

    report = run_checks(db, as_of=date(2026, 1, 6), universe=UNIVERSE)

    # Feature coverage warns (we never built features here), but nothing is an ERROR.
    assert report.ok


def test_a_symbol_missing_from_instruments_is_an_error(db):
    """It wouldn't crash — the stock would just quietly never be researched."""
    from asr.quality.checks import check_universe_resolved

    db.upsert_instruments(
        pd.DataFrame([{"instrument_key": "NSE_EQ|A", "symbol": "AAA", "isin": "I", "name": "A"}])
    )
    universe = pd.concat([UNIVERSE, pd.DataFrame([{"symbol": "GONE", "isin": "X", "name": "G"}])])

    findings = check_universe_resolved(db, universe)

    assert findings[0].severity == ERROR
    assert "GONE" in findings[0].detail
    assert "silently unresearched" in findings[0].detail
