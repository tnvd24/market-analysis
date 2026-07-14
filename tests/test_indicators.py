"""Indicator correctness.

These assert against math computed independently of pandas-ta (closed forms, textbook
definitions, series with known analytic answers). A test that just re-ran pandas-ta and
compared to itself would pass even if the library were wired up wrong — which is exactly
the failure this layer exists to prevent.
"""

import numpy as np
import pandas as pd
import pytest

from asr.features.indicators import FEATURE_COLUMNS, compute_all, compute_features


def make_candles(closes, highs=None, lows=None, volumes=None, key="NSE_EQ|A", symbol="AAA"):
    n = len(closes)
    closes = pd.Series(closes, dtype="float64")
    return pd.DataFrame(
        {
            "instrument_key": key,
            "symbol": symbol,
            "ts": pd.date_range("2024-01-01", periods=n, freq="D"),
            "open": closes,
            "high": closes + 1.0 if highs is None else pd.Series(highs, dtype="float64"),
            "low": closes - 1.0 if lows is None else pd.Series(lows, dtype="float64"),
            "close": closes,
            "volume": pd.Series(volumes if volumes is not None else [1000] * n, dtype="int64"),
        }
    )


# --- moving averages: exact closed forms ------------------------------------


def test_sma_20_equals_the_mean_of_the_last_20_closes():
    closes = list(np.arange(1, 61, dtype=float))  # 1..60
    f = compute_features(make_candles(closes))

    # SMA at the 20th bar (index 19) is mean(1..20) = 10.5
    assert f["sma_20"].iloc[19] == pytest.approx(10.5)
    # ...and at the last bar, mean(41..60) = 50.5
    assert f["sma_20"].iloc[-1] == pytest.approx(50.5)
    # warmup is NULL, never 0
    assert f["sma_20"].iloc[:19].isna().all()


def test_sma_of_a_flat_series_is_the_flat_value():
    f = compute_features(make_candles([42.0] * 60))
    assert f["sma_20"].iloc[-1] == pytest.approx(42.0)
    assert f["sma_50"].iloc[-1] == pytest.approx(42.0)


def test_ema_20_matches_the_recursive_definition():
    """EMA_t = a*close_t + (1-a)*EMA_(t-1), a = 2/(n+1), seeded on the first SMA."""
    rng = np.random.default_rng(7)
    closes = list(100 + np.cumsum(rng.normal(size=80)))
    f = compute_features(make_candles(closes))

    alpha = 2 / (20 + 1)
    ema = float(np.mean(closes[:20]))  # pandas-ta seeds with the SMA of the first window
    for c in closes[20:]:
        ema = alpha * c + (1 - alpha) * ema

    assert f["ema_20"].iloc[-1] == pytest.approx(ema, rel=1e-6)


# --- RSI: analytic edge cases -----------------------------------------------


def test_rsi_of_an_unbroken_uptrend_is_100():
    """No down moves -> average loss 0 -> RS infinite -> RSI pinned at 100."""
    f = compute_features(make_candles(list(np.arange(1, 61, dtype=float))))
    assert f["rsi_14"].iloc[-1] == pytest.approx(100.0)


def test_rsi_of_an_unbroken_downtrend_is_0():
    f = compute_features(make_candles(list(np.arange(100, 40, -1, dtype=float))))
    assert f["rsi_14"].iloc[-1] == pytest.approx(0.0)


def test_rsi_of_symmetric_moves_hovers_around_50():
    """Equal alternating gains and losses -> neither side dominates -> RSI sits near 50.

    Not *exactly* 50: Wilder smoothing is recursive and weights the most recent bar, so
    the value oscillates a point or two either side of 50 depending on whether the last
    move was up or down. The claim under test is "no directional bias", not a fixed point.
    """
    closes = [100 + (1 if i % 2 else 0) for i in range(80)]
    f = compute_features(make_candles(closes))
    assert f["rsi_14"].iloc[-1] == pytest.approx(50.0, abs=3.0)


def test_rsi_stays_inside_its_bounds():
    rng = np.random.default_rng(3)
    closes = list(100 + np.cumsum(rng.normal(size=300)))
    rsi = compute_features(make_candles(closes))["rsi_14"].dropna()
    assert rsi.between(0, 100).all()


# --- MACD: definitional identities ------------------------------------------


def test_macd_is_ema12_minus_ema26_and_hist_is_macd_minus_signal():
    rng = np.random.default_rng(11)
    closes = list(100 + np.cumsum(rng.normal(size=200)))
    f = compute_features(make_candles(closes))

    c = pd.Series(closes)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()

    # Compare late in the series, where the different EMA seeds have washed out.
    assert f["macd"].iloc[-1] == pytest.approx((ema12 - ema26).iloc[-1], abs=1e-3)
    # The histogram identity must hold exactly, at every bar.
    both = f[["macd", "macd_signal", "macd_hist"]].dropna()
    assert np.allclose(both["macd_hist"], both["macd"] - both["macd_signal"])


# --- ATR --------------------------------------------------------------------


def test_atr_of_a_constant_true_range_is_that_range():
    """Flat close, high-low always 4 -> true range always 4 -> ATR converges to 4."""
    n = 60
    closes = [100.0] * n
    f = compute_features(
        make_candles(closes, highs=[102.0] * n, lows=[98.0] * n),
    )
    assert f["atr_14"].iloc[-1] == pytest.approx(4.0)


def test_atr_is_never_negative():
    rng = np.random.default_rng(5)
    closes = 100 + np.cumsum(rng.normal(size=120))
    f = compute_features(make_candles(list(closes), highs=closes + 2, lows=closes - 3))
    assert (f["atr_14"].dropna() >= 0).all()


# --- Bollinger ---------------------------------------------------------------


def test_bollinger_mid_is_the_sma_and_bands_are_two_sigma_out():
    rng = np.random.default_rng(2)
    closes = list(100 + np.cumsum(rng.normal(size=60)))
    f = compute_features(make_candles(closes))

    window = pd.Series(closes[-20:])
    mid = window.mean()
    # pandas-ta uses the *sample* stddev (ddof=1, Bessel-corrected) for the bands.
    # Worth pinning: the population stddev is a defensible alternative, and if a future
    # release switched, every band in the warehouse would shift without any error.
    sigma = window.std(ddof=1)

    assert f["bb_mid"].iloc[-1] == pytest.approx(mid)
    assert f["bb_mid"].iloc[-1] == pytest.approx(f["sma_20"].iloc[-1])
    assert f["bb_upper"].iloc[-1] == pytest.approx(mid + 2 * sigma, rel=1e-6)
    assert f["bb_lower"].iloc[-1] == pytest.approx(mid - 2 * sigma, rel=1e-6)


# --- volume / returns --------------------------------------------------------


def test_rel_volume_is_volume_over_its_20_day_average():
    volumes = [1000] * 39 + [3000]  # last bar spikes; prior 20-day mean is 1000
    f = compute_features(make_candles([100.0] * 40, volumes=volumes))

    assert f["volume_sma_20"].iloc[-1] == pytest.approx((19 * 1000 + 3000) / 20)
    assert f["rel_volume"].iloc[-1] == pytest.approx(3000 / ((19 * 1000 + 3000) / 20))


def test_ret_1d_is_the_simple_daily_return():
    f = compute_features(make_candles([100.0, 110.0, 99.0]))
    assert pd.isna(f["ret_1d"].iloc[0])  # no prior bar to compare against
    assert f["ret_1d"].iloc[1] == pytest.approx(0.10)
    assert f["ret_1d"].iloc[2] == pytest.approx(-0.10)


# --- structural guarantees ---------------------------------------------------


def test_output_is_one_row_per_candle_with_the_declared_schema():
    f = compute_features(make_candles(list(np.arange(1, 31, dtype=float))))
    assert len(f) == 30
    assert list(f.columns) == ["instrument_key", "symbol", "ts", *FEATURE_COLUMNS]


def test_short_history_leaves_slow_indicators_null_rather_than_wrong():
    f = compute_features(make_candles(list(np.arange(1, 31, dtype=float))))  # 30 bars
    assert f["sma_20"].notna().any()  # 20-day fits
    assert f["sma_50"].isna().all()  # 50-day does not — must be NULL, not extrapolated
    assert f["sma_200"].isna().all()


def test_compute_all_never_lets_a_window_span_two_instruments():
    """The bug this guards: B's SMA silently averaging in A's prices."""
    a = make_candles([10.0] * 30, key="NSE_EQ|A", symbol="AAA")
    b = make_candles([1000.0] * 30, key="NSE_EQ|B", symbol="BBB")

    f = compute_all(pd.concat([a, b], ignore_index=True))

    a_last = f[f["instrument_key"] == "NSE_EQ|A"].iloc[-1]
    b_last = f[f["instrument_key"] == "NSE_EQ|B"].iloc[-1]
    assert a_last["sma_20"] == pytest.approx(10.0)  # untainted by B's 1000s
    assert b_last["sma_20"] == pytest.approx(1000.0)


def test_row_order_does_not_change_the_answer():
    """Candles arriving shuffled must still produce time-ordered indicators."""
    closes = list(np.arange(1, 61, dtype=float))
    ordered = make_candles(closes)
    shuffled = ordered.sample(frac=1, random_state=1)

    assert compute_features(shuffled)["sma_20"].iloc[-1] == pytest.approx(
        compute_features(ordered)["sma_20"].iloc[-1]
    )


def test_empty_input_returns_an_empty_frame_with_the_schema():
    f = compute_features(pd.DataFrame(columns=["instrument_key", "symbol", "ts", "close"]))
    assert f.empty
    assert "rsi_14" in f.columns
