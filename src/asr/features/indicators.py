"""Phase 3: deterministic indicators (RSI, MACD, MAs, ATR, Bollinger, volume).

Real math via pandas-ta — never LLM-guessed. Reads ``candles``, writes ``features``.

Two rules hold this layer together:

1. **Stable column names.** pandas-ta names its output after its parameters
   (``MACDh_12_26_9``, ``BBL_20_2.0_2.0``). We rename to fixed names (``macd_hist``,
   ``bb_lower``) so that retuning a parameter, or a pandas-ta upgrade, never renames a
   column that a Phase 5 agent tool is querying.
2. **Never compute across instruments.** Indicators are stateful along time; a rolling
   window that spans two stocks is silent nonsense. :func:`compute_features` handles one
   instrument, and :func:`compute_all` is the only thing that groups.

Warmup rows (the first ``length-1`` bars of any window) are NULL, not zero — a missing
indicator must read as missing all the way to the agent layer.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .schema import FEATURE_COLUMNS, MIN_HISTORY

__all__ = ["FEATURE_COLUMNS", "MIN_HISTORY", "compute_all", "compute_features"]


def _by_prefix(df: pd.DataFrame, prefix: str) -> pd.Series:
    """Select a pandas-ta column by prefix — its suffixes drift between releases."""
    for col in df.columns:
        if col.startswith(prefix):
            return df[col]
    raise KeyError(f"pandas-ta returned no column starting with {prefix!r}: {list(df.columns)}")


def compute_features(candles: pd.DataFrame) -> pd.DataFrame:
    """Indicators for **one** instrument's candles.

    Expects ``instrument_key, symbol, ts, open, high, low, close, volume``.
    Returns one row per input candle, sorted by ts, with :data:`FEATURE_COLUMNS`.
    """
    if candles.empty:
        return pd.DataFrame(columns=["instrument_key", "symbol", "ts", *FEATURE_COLUMNS])

    df = candles.sort_values("ts").reset_index(drop=True)
    close, high, low = df["close"], df["high"], df["low"]
    volume = df["volume"].astype("float64")

    out = df[["instrument_key", "symbol", "ts"]].copy()

    out["rsi_14"] = ta.rsi(close, length=14)

    macd = ta.macd(close, fast=12, slow=26, signal=9)
    if macd is not None:
        out["macd"] = _by_prefix(macd, "MACD_")
        out["macd_signal"] = _by_prefix(macd, "MACDs_")
        out["macd_hist"] = _by_prefix(macd, "MACDh_")

    out["sma_20"] = ta.sma(close, length=20)
    out["sma_50"] = ta.sma(close, length=50)
    out["sma_200"] = ta.sma(close, length=200)
    out["ema_20"] = ta.ema(close, length=20)

    out["atr_14"] = ta.atr(high, low, close, length=14)

    bb = ta.bbands(close, length=20, std=2.0)
    if bb is not None:
        out["bb_lower"] = _by_prefix(bb, "BBL_")
        out["bb_mid"] = _by_prefix(bb, "BBM_")
        out["bb_upper"] = _by_prefix(bb, "BBU_")
        out["bb_pct"] = _by_prefix(bb, "BBP_")

    out["obv"] = ta.obv(close, volume)

    out["ret_1d"] = close.pct_change()
    out["volume_sma_20"] = ta.sma(volume, length=20)
    # Today's volume against its 20-day norm: 2.0 means "twice the usual interest".
    out["rel_volume"] = volume / out["volume_sma_20"]

    return out.reindex(columns=["instrument_key", "symbol", "ts", *FEATURE_COLUMNS])


def compute_all(candles: pd.DataFrame) -> pd.DataFrame:
    """Indicators for many instruments — grouped, so no window ever spans two stocks."""
    if candles.empty:
        return pd.DataFrame(columns=["instrument_key", "symbol", "ts", *FEATURE_COLUMNS])
    frames = [compute_features(g) for _, g in candles.groupby("instrument_key", sort=False)]
    return pd.concat(frames, ignore_index=True)
