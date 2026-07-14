"""Rule-based strategies, as position series.

Each takes a feature frame (indicators, one row per bar, already computed on **adjusted**
prices) and returns a position: 1 = long, 0 = flat. Long-only, no shorting, no leverage —
this is a research tool, and a backtest that quietly assumed shorting would flatter every
mean-reversion rule.

The rules here deliberately mirror `features/signals.py`, so what the research pack *shows*
you is what the backtest *measures*. A strategy that didn't exist as a signal would be
untestable in practice, and a signal never backtested would be a story.

**A position is decided from the bar's close and acted on the next bar.** The engine enforces
that shift; nothing here may peek. See `engine.py`.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd

from ..features.signals import RSI_OVERBOUGHT, RSI_OVERSOLD


def _flat(index) -> pd.Series:
    return pd.Series(0.0, index=index)


def sma_cross(f: pd.DataFrame) -> pd.Series:
    """Long while the 50-day is above the 200-day (the golden-cross regime)."""
    if "sma_50" not in f or "sma_200" not in f:
        return _flat(f.index)
    return (f["sma_50"] > f["sma_200"]).astype(float).where(f["sma_200"].notna(), 0.0)


def trend_200(f: pd.DataFrame) -> pd.Series:
    """Long while the close is above its own 200-day average. The simplest trend filter."""
    if "close" not in f or "sma_200" not in f:
        return _flat(f.index)
    return (f["close"] > f["sma_200"]).astype(float).where(f["sma_200"].notna(), 0.0)


def macd_cross(f: pd.DataFrame) -> pd.Series:
    """Long while MACD is above its signal line."""
    if "macd" not in f or "macd_signal" not in f:
        return _flat(f.index)
    return (f["macd"] > f["macd_signal"]).astype(float).where(f["macd"].notna(), 0.0)


def rsi_reversion(f: pd.DataFrame) -> pd.Series:
    """Buy oversold, sell when it recovers past overbought. Stateful, so it needs a loop.

    This is the rule people *assume* works ("buy the dip"). Backtesting it is the point:
    an assumption with a number attached stops being an assumption.
    """
    if "rsi_14" not in f:
        return _flat(f.index)

    pos, holding = [], False
    for rsi in f["rsi_14"]:
        if pd.isna(rsi):
            pos.append(0.0)
            continue
        if not holding and rsi < RSI_OVERSOLD:
            holding = True
        elif holding and rsi > RSI_OVERBOUGHT:
            holding = False
        pos.append(1.0 if holding else 0.0)
    return pd.Series(pos, index=f.index)


def buy_and_hold(f: pd.DataFrame) -> pd.Series:
    """The benchmark that most strategies quietly fail to beat."""
    return pd.Series(1.0, index=f.index)


STRATEGIES: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "sma_cross": sma_cross,
    "trend_200": trend_200,
    "macd_cross": macd_cross,
    "rsi_reversion": rsi_reversion,
    "buy_and_hold": buy_and_hold,
}
